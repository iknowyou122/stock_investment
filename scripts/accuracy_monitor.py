"""Signal accuracy monitor.

Tracks win-rate and accuracy metrics for historical scan signals by cross-referencing
scan_*.csv files with actual OHLCV outcomes.

Usage:
    python scripts/accuracy_monitor.py                         # Show recent dashboard
    python scripts/accuracy_monitor.py --date 2026-04-15      # Query specific date
    python scripts/accuracy_monitor.py --industry 半導體 --top 10
    python scripts/accuracy_monitor.py --export report.csv    # Export CSV report
    make monitor
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)

_console = Console()
_lock = Lock()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_SCANS_DIR = _ROOT / "data" / "scans"
_CONFIG_DIR = _ROOT / "config"
_WATCHLIST_CACHE_DIR = _ROOT / "data" / "watchlist_cache"
_CACHE_PATH = _CONFIG_DIR / "signal_outcomes_cache.json"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BREAKOUT_THRESHOLD = 0.99   # close >= twenty_day_high * 0.99
_OUTCOME_WINDOW = 10          # T+1 to T+10 trading bars
_PENDING_CUTOFF_DAYS = 10    # signals within T+10 calendar days are still pending

# Confidence tier buckets
CONFIDENCE_TIERS = ["0-39", "40-49", "50-59", "60-69", "70-79", "80+"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SignalRecord:
    signal_id: str
    ticker: str
    signal_date: date
    confidence: int
    action: str
    market: str
    industry: str
    entry_price: float
    twenty_day_high: float
    actual_breakout: bool
    days_to_breakout: int
    max_price: float
    upside_pct: float
    pending: bool = False


# ---------------------------------------------------------------------------
# Pure metric functions (no I/O — fully testable)
# ---------------------------------------------------------------------------

def _confidence_to_tier(score: int) -> str:
    """Map a confidence score to its display tier bucket string."""
    if score < 40:
        return "0-39"
    elif score < 50:
        return "40-49"
    elif score < 60:
        return "50-59"
    elif score < 70:
        return "60-69"
    elif score < 80:
        return "70-79"
    else:
        return "80+"


def compute_win_rate(
    records: list[SignalRecord],
) -> tuple[Optional[float], int]:
    """Compute win-rate excluding pending records.

    Returns (win_rate, n) where win_rate is None when n == 0.
    """
    settled = [r for r in records if not r.pending]
    n = len(settled)
    if n == 0:
        return None, 0
    wins = sum(1 for r in settled if r.actual_breakout)
    return wins / n, n


def compute_rolling_win_rate(
    records: list[SignalRecord],
    window: int,
) -> Optional[float]:
    """Compute win-rate over the last `window` settled records (sorted by date).

    Returns None if there are fewer than `window` settled records available.
    Records should be sorted by signal_date ascending before calling.
    """
    settled = [r for r in records if not r.pending]
    settled_sorted = sorted(settled, key=lambda r: r.signal_date)
    if len(settled_sorted) < window:
        return None
    last_n = settled_sorted[-window:]
    wins = sum(1 for r in last_n if r.actual_breakout)
    return wins / window


def stratify_by_field(
    records: list[SignalRecord],
    field: str,
) -> dict[str, tuple[Optional[float], int]]:
    """Group records by a string field, return {value: (win_rate, n)}.

    Pending records are excluded from win-rate numerator/denominator.
    Field can be 'industry', 'market', or 'confidence_tier'.
    """
    groups: dict[str, list[SignalRecord]] = {}
    for rec in records:
        key = getattr(rec, field)
        groups.setdefault(key, []).append(rec)

    result: dict[str, tuple[Optional[float], int]] = {}
    for key, group in groups.items():
        wr, n = compute_win_rate(group)
        result[key] = (wr, n)
    return result


# ---------------------------------------------------------------------------
# Cache store
# ---------------------------------------------------------------------------

class CacheStore:
    """JSON file-backed store for SignalRecord objects.

    Thread-safe for single-process use via _lock (acquired externally by callers
    that use _lock for the outer iteration).
    """

    def __init__(self, path: Path = _CACHE_PATH) -> None:
        self._path = path
        self._records: dict[str, SignalRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            for sig_id, d in raw.get("signals", {}).items():
                self._records[sig_id] = SignalRecord(
                    signal_id=sig_id,
                    ticker=d["ticker"],
                    signal_date=date.fromisoformat(d["signal_date"]),
                    confidence=int(d["confidence"]),
                    action=d.get("action", "LONG"),
                    market=d.get("market", "TSE"),
                    industry=d.get("industry", "未知"),
                    entry_price=float(d.get("entry_price", 0.0)),
                    twenty_day_high=float(d.get("twenty_day_high", 0.0)),
                    actual_breakout=bool(d.get("actual_breakout", False)),
                    days_to_breakout=int(d.get("days_to_breakout", 0)),
                    max_price=float(d.get("max_price", 0.0)),
                    upside_pct=float(d.get("upside_pct", 0.0)),
                    pending=bool(d.get("pending", False)),
                )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("CacheStore: failed to load %s: %s", self._path, e)

    def get(self, signal_id: str) -> Optional[SignalRecord]:
        return self._records.get(signal_id)

    def upsert(self, record: SignalRecord) -> None:
        self._records[record.signal_id] = record

    def all(self) -> list[SignalRecord]:
        return list(self._records.values())

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        signals_dict: dict[str, dict] = {}
        with _lock:
            items = list(self._records.items())
        for sig_id, rec in items:
            signals_dict[sig_id] = {
                "ticker": rec.ticker,
                "signal_date": rec.signal_date.isoformat(),
                "confidence": rec.confidence,
                "action": rec.action,
                "market": rec.market,
                "industry": rec.industry,
                "entry_price": rec.entry_price,
                "twenty_day_high": rec.twenty_day_high,
                "actual_breakout": rec.actual_breakout,
                "days_to_breakout": rec.days_to_breakout,
                "max_price": rec.max_price,
                "upside_pct": rec.upside_pct,
                "pending": rec.pending,
            }
        payload = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "signals": signals_dict,
        }
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Scan CSV loading
# ---------------------------------------------------------------------------

def _load_industry_map() -> dict[str, str]:
    """Load the most recent cached industry map (ticker → industry)."""
    today = date.today()
    for delta in range(0, 14):
        candidate = today - timedelta(days=delta)
        path = _WATCHLIST_CACHE_DIR / f"industry_map_{candidate}.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("_load_industry_map: failed to read %s: %s", path, e)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from batch_plan import _build_industry_map  # type: ignore[import]
        return _build_industry_map() or {}
    except Exception:
        return {}


def _load_market_map() -> dict[str, str]:
    """Load the most recent cached market map (ticker → TSE/TPEx)."""
    today = date.today()
    for delta in range(0, 14):
        candidate = today - timedelta(days=delta)
        path = _WATCHLIST_CACHE_DIR / f"market_map_{candidate}.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
    return {}


def load_scan_signals(
    scan_dir: Path = _SCANS_DIR,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    min_confidence: int = 0,
    industry_filter: Optional[str] = None,
) -> list[dict]:
    """Load signals from scan_*.csv files.

    Returns list of dicts with keys:
      signal_id, ticker, signal_date, confidence, action, entry_price,
      industry, market.
    Deduplicates (ticker, signal_date) keeping highest confidence.
    """
    if not scan_dir.exists():
        return []

    industry_map = _load_industry_map()
    market_map = _load_market_map()

    seen: dict[tuple[str, date], dict] = {}

    for csv_path in sorted(scan_dir.glob("scan_*.csv")):
        try:
            file_date_str = csv_path.stem.replace("scan_", "")
            file_date = date.fromisoformat(file_date_str)
        except ValueError:
            continue

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                action = row.get("action", "CAUTION")
                if action == "CAUTION":
                    continue

                ticker = row.get("ticker", "").strip()
                if not ticker:
                    continue

                try:
                    confidence = int(float(row.get("confidence", 0) or 0))
                except (ValueError, TypeError):
                    confidence = 0

                if confidence < min_confidence:
                    continue

                # Resolve signal date
                raw_date = row.get("analysis_date") or row.get("scan_date")
                try:
                    sig_date = date.fromisoformat(raw_date) if raw_date else file_date
                except ValueError:
                    sig_date = file_date

                if date_from and sig_date < date_from:
                    continue
                if date_to and sig_date > date_to:
                    continue

                industry = industry_map.get(ticker, "未知")
                if industry_filter and industry != industry_filter:
                    continue

                market = market_map.get(ticker, "TSE")

                try:
                    entry_price = float(row.get("entry_bid") or 0.0)
                except (ValueError, TypeError):
                    entry_price = 0.0

                key = (ticker, sig_date)
                existing = seen.get(key)
                if existing is None or confidence > existing["confidence"]:
                    seen[key] = {
                        "signal_id": f"{ticker}_{sig_date.isoformat()}",
                        "ticker": ticker,
                        "signal_date": sig_date,
                        "confidence": confidence,
                        "action": action,
                        "entry_price": entry_price,
                        "industry": industry,
                        "market": market,
                    }

    return sorted(seen.values(), key=lambda x: (x["signal_date"], x["ticker"]))


# ---------------------------------------------------------------------------
# OHLCV helpers (same pattern as backtest_v23_vs_v22.py)
# ---------------------------------------------------------------------------

def _df_to_ohlcv_list(df, ticker: str) -> list:
    """Convert DataFrame to sorted list of simple bar dicts."""
    bars = []
    for _, row in df.iterrows():
        bars.append({
            "ticker": ticker,
            "trade_date": row["trade_date"],
            "close": float(row["close"]),
            "high": float(row["high"]),
            "volume": int(row["volume"]),
        })
    return sorted(bars, key=lambda x: x["trade_date"])


def _fetch_future_bars(
    ticker: str,
    signal_date: date,
    finmind: FinMindClient,
    window: int = _OUTCOME_WINDOW,
) -> list[dict]:
    """Fetch T+1 to T+window bars for outcome evaluation."""
    start = signal_date + timedelta(days=1)
    end = signal_date + timedelta(days=window + 7)  # calendar buffer
    try:
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        bars = _df_to_ohlcv_list(df, ticker)
        bars = [b for b in bars if b["trade_date"] > signal_date]
        return bars[:window]
    except Exception as e:
        logger.debug("_fetch_future_bars %s %s: %s", ticker, signal_date, e)
        return []


def _fetch_signal_date_bar(
    ticker: str,
    signal_date: date,
    finmind: FinMindClient,
) -> Optional[dict]:
    """Fetch the OHLCV bar for signal_date (to get entry_price / 20d_high)."""
    start = signal_date - timedelta(days=35)
    try:
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=signal_date)
        if df is None or df.empty:
            return None
        bars = _df_to_ohlcv_list(df, ticker)
        bars_up_to = [b for b in bars if b["trade_date"] <= signal_date]
        if not bars_up_to:
            return None
        last_bar = bars_up_to[-1]
        last20 = bars_up_to[-20:]
        twenty_day_high = max(b["high"] for b in last20) if last20 else last_bar["close"]
        return {
            "close": last_bar["close"],
            "twenty_day_high": twenty_day_high,
        }
    except Exception as e:
        logger.debug("_fetch_signal_date_bar %s %s: %s", ticker, signal_date, e)
        return None


def _evaluate_outcome(
    entry_price: float,
    twenty_day_high: float,
    future_bars: list[dict],
) -> tuple[bool, int, float, float]:
    """Evaluate whether a breakout occurred.

    Returns (actual_breakout, days_to_breakout, max_price, upside_pct).
    """
    if not future_bars:
        return False, 0, entry_price, 0.0

    threshold = twenty_day_high * _BREAKOUT_THRESHOLD
    max_close = max(b["close"] for b in future_bars)
    upside_pct = (max_close / entry_price - 1) * 100 if entry_price > 0 else 0.0

    for i, bar in enumerate(future_bars, start=1):
        if bar["close"] >= threshold:
            return True, i, max_close, upside_pct

    return False, len(future_bars), max_close, upside_pct


# ---------------------------------------------------------------------------
# Incremental outcome checker
# ---------------------------------------------------------------------------

def _is_pending(signal_date: date) -> bool:
    """Return True if the T+10 window has not yet closed."""
    return (date.today() - signal_date).days < _PENDING_CUTOFF_DAYS


def check_and_update_outcomes(
    signals: list[dict],
    cache: CacheStore,
    finmind: FinMindClient,
    workers: int = 4,
    force_recheck: bool = False,
) -> CacheStore:
    """For each signal, verify or skip based on cache.

    Signals already in cache that are settled (not pending) are skipped unless
    force_recheck=True. Pending signals and new signals are fetched.
    """
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")

    to_fetch: list[dict] = []

    for sig in signals:
        sid = sig["signal_id"]
        cached = cache.get(sid)
        if cached is not None and not cached.pending and not force_recheck:
            continue  # Already settled — skip
        to_fetch.append(sig)

    if not to_fetch:
        _console.print("  [dim]所有信號已有快取結果，跳過 API 查詢。[/dim]")
        return cache

    _console.print(
        f"  [dim]查詢 {len(to_fetch)} 個信號結果"
        f"（共 {len(signals)} 個，跳過 {len(signals) - len(to_fetch)} 個已結算信號）[/dim]"
    )

    def _process_signal(sig: "dict[str, object]") -> Optional[SignalRecord]:
        time.sleep(0.12)  # gentle rate limiting

        ticker = sig["ticker"]
        signal_date = sig["signal_date"]
        pending = _is_pending(signal_date)  # type: ignore[arg-type]

        # Always fetch signal-date bar (for entry price and 20d high),
        # even for pending signals — we need twenty_day_high for the breakout
        # threshold and want it stored so we don't re-fetch after settlement.
        entry_price = sig.get("entry_price", 0.0)
        twenty_day_high = 0.0

        if entry_price <= 0 or twenty_day_high <= 0:
            bar_info = _fetch_signal_date_bar(ticker, signal_date, finmind)  # type: ignore[arg-type]
            if bar_info:
                if entry_price <= 0:
                    entry_price = bar_info["close"]
                twenty_day_high = bar_info["twenty_day_high"]

        if twenty_day_high <= 0:
            logger.debug("No twenty_day_high for %s %s", ticker, signal_date)
            # For pending signals we still want a placeholder; for settled ones
            # we cannot evaluate without a reference price so skip.
            if pending and not force_recheck:
                return SignalRecord(
                    signal_id=sig["signal_id"],  # type: ignore[arg-type]
                    ticker=ticker,  # type: ignore[arg-type]
                    signal_date=signal_date,  # type: ignore[arg-type]
                    confidence=sig["confidence"],  # type: ignore[arg-type]
                    action=sig["action"],  # type: ignore[arg-type]
                    market=sig["market"],  # type: ignore[arg-type]
                    industry=sig["industry"],  # type: ignore[arg-type]
                    entry_price=entry_price,  # type: ignore[arg-type]
                    twenty_day_high=0.0,
                    actual_breakout=False,
                    days_to_breakout=0,
                    max_price=0.0,
                    upside_pct=0.0,
                    pending=True,
                )
            return None

        # If pending and not forcing, create a pending placeholder (with price data)
        if pending and not force_recheck:
            return SignalRecord(
                signal_id=sig["signal_id"],  # type: ignore[arg-type]
                ticker=ticker,  # type: ignore[arg-type]
                signal_date=signal_date,  # type: ignore[arg-type]
                confidence=sig["confidence"],  # type: ignore[arg-type]
                action=sig["action"],  # type: ignore[arg-type]
                market=sig["market"],  # type: ignore[arg-type]
                industry=sig["industry"],  # type: ignore[arg-type]
                entry_price=entry_price,  # type: ignore[arg-type]
                twenty_day_high=twenty_day_high,
                actual_breakout=False,
                days_to_breakout=0,
                max_price=0.0,
                upside_pct=0.0,
                pending=True,
            )

        # Fetch future bars
        future_bars = _fetch_future_bars(ticker, signal_date, finmind)

        actual_breakout, days_to_breakout, max_price, upside_pct = _evaluate_outcome(
            entry_price, twenty_day_high, future_bars
        )

        return SignalRecord(
            signal_id=sig["signal_id"],
            ticker=ticker,
            signal_date=signal_date,
            confidence=sig["confidence"],
            action=sig["action"],
            market=sig["market"],
            industry=sig["industry"],
            entry_price=entry_price,
            twenty_day_high=twenty_day_high,
            actual_breakout=actual_breakout,
            days_to_breakout=days_to_breakout,
            max_price=max_price,
            upside_pct=upside_pct,
            pending=pending,
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("驗證信號結果...", total=len(to_fetch))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_signal, sig): sig for sig in to_fetch}
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result is not None:
                        with _lock:
                            cache.upsert(result)
                except Exception as e:
                    sig = futures[future]
                    logger.debug("Failed to process signal %s: %s", sig.get("signal_id"), e)

    return cache


# ---------------------------------------------------------------------------
# Rich dashboard
# ---------------------------------------------------------------------------

def _fmt_wr(wr: Optional[float], n: int) -> str:
    if wr is None:
        return "[dim]—[/dim]"
    pct = wr * 100
    color = "green" if pct >= 55 else ("yellow" if pct >= 45 else "red")
    return f"[{color}]{pct:.1f}%[/{color}] [dim]({n})[/dim]"


def _fmt_pct(v: float) -> str:
    color = "green" if v >= 0 else "red"
    return f"[{color}]{v:+.1f}%[/{color}]"


def _rolling_str(records: list[SignalRecord], window: int) -> str:
    wr = compute_rolling_win_rate(records, window)
    if wr is None:
        return "[dim]N/A[/dim]"
    pct = wr * 100
    color = "green" if pct >= 55 else ("yellow" if pct >= 45 else "red")
    return f"[{color}]{pct:.1f}%[/{color}]"


def _trend_arrow(records: list[SignalRecord]) -> str:
    """Show a rough recent-vs-older trend indicator."""
    settled = sorted([r for r in records if not r.pending], key=lambda r: r.signal_date)
    if len(settled) < 20:
        return "[dim]—[/dim]"
    half = len(settled) // 2
    older = settled[:half]
    newer = settled[half:]
    wr_old = sum(1 for r in older if r.actual_breakout) / len(older)
    wr_new = sum(1 for r in newer if r.actual_breakout) / len(newer)
    diff = wr_new - wr_old
    if diff > 0.05:
        return "[green]↑ 改善[/green]"
    elif diff < -0.05:
        return "[red]↓ 下降[/red]"
    return "[yellow]→ 平穩[/yellow]"


def render_dashboard(
    records: list[SignalRecord],
    top_n: int = 10,
    date_filter: Optional[date] = None,
    industry_filter: Optional[str] = None,
) -> None:
    """Render the accuracy dashboard to the console."""

    # Apply filters
    filtered = records
    if date_filter:
        filtered = [r for r in filtered if r.signal_date == date_filter]
    if industry_filter:
        filtered = [r for r in filtered if r.industry == industry_filter]

    settled = [r for r in filtered if not r.pending]
    pending_count = sum(1 for r in filtered if r.pending)

    # ---------------------------------------------------------------------------
    # Header panel
    # ---------------------------------------------------------------------------
    overall_wr, total_n = compute_win_rate(filtered)
    wr_str = f"{overall_wr * 100:.1f}%" if overall_wr is not None else "N/A"
    trend_str = _trend_arrow(filtered)

    _console.print(Panel(
        f"[bold white]信號準確度監控[/bold white]\n"
        f"[dim]總信號：{len(filtered)}  |  已結算：{total_n}  |  待觀察：{pending_count}[/dim]\n"
        f"整體勝率：[bold cyan]{wr_str}[/bold cyan]  趨勢：{trend_str}",
        title="[bold magenta]Accuracy Monitor[/bold magenta]",
        border_style="magenta",
        padding=(0, 2),
    ))

    # ---------------------------------------------------------------------------
    # Rolling metrics
    # ---------------------------------------------------------------------------
    _console.print("\n[bold]滾動勝率[/bold]")
    rolling_tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
    rolling_tbl.add_column("窗口", width=10)
    rolling_tbl.add_column("勝率", justify="right", width=12)

    for window in [20, 50, 100]:
        label = f"最近 {window} 信號"
        rolling_tbl.add_row(label, _rolling_str(filtered, window))

    _console.print(rolling_tbl)

    # ---------------------------------------------------------------------------
    # Stratified by industry
    # ---------------------------------------------------------------------------
    industry_stats = stratify_by_field(settled, "industry")
    # Sort by win-rate desc, then by total count desc
    sorted_industries = sorted(
        industry_stats.items(),
        key=lambda x: (-(x[1][0] or 0), -x[1][1]),
    )

    ind_tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        title="[bold]產業勝率[/bold]",
        title_style="bold white",
    )
    ind_tbl.add_column("產業", width=14)
    ind_tbl.add_column("N", justify="right", width=6)
    ind_tbl.add_column("勝率", justify="right", width=12)

    for industry, (wr, n) in sorted_industries[:top_n]:
        ind_tbl.add_row(industry, str(n), _fmt_wr(wr, n))

    _console.print(ind_tbl)

    # ---------------------------------------------------------------------------
    # Stratified by market
    # ---------------------------------------------------------------------------
    market_stats = stratify_by_field(settled, "market")
    mkt_tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        title="[bold]市場別勝率[/bold]",
        title_style="bold white",
    )
    mkt_tbl.add_column("市場", width=8)
    mkt_tbl.add_column("N", justify="right", width=6)
    mkt_tbl.add_column("勝率", justify="right", width=12)

    for market in ["TSE", "TPEx"]:
        if market not in market_stats:
            continue
        wr, n = market_stats[market]
        mkt_tbl.add_row(market, str(n), _fmt_wr(wr, n))

    _console.print(mkt_tbl)

    # ---------------------------------------------------------------------------
    # Stratified by confidence tier
    # ---------------------------------------------------------------------------
    tier_groups: dict[str, list[SignalRecord]] = {}
    for rec in settled:
        t = _confidence_to_tier(rec.confidence)
        tier_groups.setdefault(t, []).append(rec)

    tier_tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold yellow",
        border_style="dim",
        title="[bold]信心分層勝率[/bold]",
        title_style="bold white",
    )
    tier_tbl.add_column("信心層", width=10)
    tier_tbl.add_column("N", justify="right", width=6)
    tier_tbl.add_column("勝率", justify="right", width=12)

    for tier in CONFIDENCE_TIERS:
        group = tier_groups.get(tier, [])
        if not group:
            continue
        wr, n = compute_win_rate(group)
        tier_tbl.add_row(tier, str(n), _fmt_wr(wr, n))

    _console.print(tier_tbl)

    # ---------------------------------------------------------------------------
    # Top performers and worst performers
    # ---------------------------------------------------------------------------
    sorted_settled = sorted(settled, key=lambda r: r.upside_pct, reverse=True)

    if sorted_settled:
        _console.print("\n[bold]Top performers[/bold]")
        top_tbl = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold green",
            border_style="dim",
        )
        top_tbl.add_column("日期", width=12)
        top_tbl.add_column("代號", width=8)
        top_tbl.add_column("產業", width=12)
        top_tbl.add_column("信心", justify="right", width=6)
        top_tbl.add_column("進場", justify="right", width=8)
        top_tbl.add_column("最高報酬", justify="right", width=10)
        top_tbl.add_column("突破", width=6)

        for rec in sorted_settled[:5]:
            brk_str = "[green]✓[/green]" if rec.actual_breakout else "[red]✗[/red]"
            top_tbl.add_row(
                rec.signal_date.isoformat(),
                rec.ticker,
                rec.industry[:10],
                str(rec.confidence),
                f"{rec.entry_price:.2f}",
                _fmt_pct(rec.upside_pct),
                brk_str,
            )

        _console.print(top_tbl)

    worst = [r for r in sorted_settled if not r.actual_breakout]
    worst_sorted = sorted(worst, key=lambda r: r.upside_pct)[:5]

    if worst_sorted:
        _console.print("\n[bold]Worst performers (non-breakout)[/bold]")
        worst_tbl = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold red",
            border_style="dim",
        )
        worst_tbl.add_column("日期", width=12)
        worst_tbl.add_column("代號", width=8)
        worst_tbl.add_column("產業", width=12)
        worst_tbl.add_column("信心", justify="right", width=6)
        worst_tbl.add_column("進場", justify="right", width=8)
        worst_tbl.add_column("最高報酬", justify="right", width=10)

        for rec in worst_sorted:
            worst_tbl.add_row(
                rec.signal_date.isoformat(),
                rec.ticker,
                rec.industry[:10],
                str(rec.confidence),
                f"{rec.entry_price:.2f}",
                _fmt_pct(rec.upside_pct),
            )

        _console.print(worst_tbl)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_EXPORT_FIELDS = [
    "signal_id",
    "signal_date",
    "ticker",
    "market",
    "industry",
    "confidence",
    "confidence_tier",
    "action",
    "entry_price",
    "twenty_day_high",
    "actual_breakout",
    "days_to_breakout",
    "max_price",
    "upside_pct",
    "pending",
]


def export_csv(records: list[SignalRecord], output_path: Path) -> None:
    """Write all records to a CSV file with stratification columns."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for rec in sorted(records, key=lambda r: (r.signal_date, r.ticker)):
            writer.writerow({
                "signal_id": rec.signal_id,
                "signal_date": rec.signal_date.isoformat(),
                "ticker": rec.ticker,
                "market": rec.market,
                "industry": rec.industry,
                "confidence": rec.confidence,
                "confidence_tier": _confidence_to_tier(rec.confidence),
                "action": rec.action,
                "entry_price": rec.entry_price,
                "twenty_day_high": rec.twenty_day_high,
                "actual_breakout": rec.actual_breakout,
                "days_to_breakout": rec.days_to_breakout,
                "max_price": rec.max_price,
                "upside_pct": round(rec.upside_pct, 4),
                "pending": rec.pending,
            })
    _console.print(f"\n  [green]CSV 已匯出：[/green]{output_path}  ({len(records)} 筆)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="信號準確度監控 — 載入歷史 scan CSV，驗證突破結果，顯示滾動勝率 Dashboard"
    )
    parser.add_argument("--date", default=None, help="查詢特定日期的信號 YYYY-MM-DD")
    parser.add_argument("--industry", default=None, help="只顯示特定產業")
    parser.add_argument("--top", type=int, default=10, help="產業表顯示前 N 名（預設 10）")
    parser.add_argument("--export", default=None, metavar="FILE", help="匯出 CSV 到指定路徑")
    parser.add_argument(
        "--date-from",
        default=None,
        help="載入信號起始日 YYYY-MM-DD（預設：90天前）",
    )
    parser.add_argument(
        "--date-to",
        default=None,
        help="載入信號結束日 YYYY-MM-DD（預設：今天）",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=40,
        help="最低信心門檻（預設 40）",
    )
    parser.add_argument(
        "--force-recheck",
        action="store_true",
        help="強制重新驗證所有信號（忽略快取）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="並行 worker 數（預設 4）",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="不查詢 API，只讀快取並顯示（快速預覽）",
    )
    args = parser.parse_args()

    date_from: Optional[date] = None
    date_to: Optional[date] = None

    if args.date_from:
        date_from = date.fromisoformat(args.date_from)
    else:
        date_from = date.today() - timedelta(days=90)

    if args.date_to:
        date_to = date.fromisoformat(args.date_to)
    else:
        date_to = date.today()

    date_filter = date.fromisoformat(args.date) if args.date else None

    _console.print(Panel(
        f"[bold white]信號準確度監控[/bold white]\n"
        f"[dim]載入區間：{date_from} → {date_to}  |  最低信心：{args.min_confidence}[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # Load cache
    cache = CacheStore(_CACHE_PATH)

    if not args.no_fetch:
        # Load historical signals from CSV
        _console.print("[dim]載入歷史掃描信號...[/dim]")
        signals = load_scan_signals(
            scan_dir=_SCANS_DIR,
            date_from=date_from,
            date_to=date_to,
            min_confidence=args.min_confidence,
            industry_filter=args.industry,
        )
        _console.print(f"  [dim]載入 [bold]{len(signals)}[/bold] 個信號[/dim]")

        if signals:
            finmind = FinMindClient()
            cache = check_and_update_outcomes(
                signals=signals,
                cache=cache,
                finmind=finmind,
                workers=args.workers,
                force_recheck=args.force_recheck,
            )
            cache.save()
            _console.print(f"  [dim]快取已儲存：{_CACHE_PATH}[/dim]")

    # Gather all records in range for display
    all_records = cache.all()
    display_records = [
        r for r in all_records
        if (date_from is None or r.signal_date >= date_from)
        and (date_to is None or r.signal_date <= date_to)
        and r.confidence >= args.min_confidence
    ]
    if args.industry:
        display_records = [r for r in display_records if r.industry == args.industry]

    if not display_records:
        _console.print("[yellow]沒有符合條件的信號記錄。[/yellow]")
        return

    # Render dashboard
    render_dashboard(
        records=display_records,
        top_n=args.top,
        date_filter=date_filter,
        industry_filter=None,  # already filtered above
    )

    # Optional export
    if args.export:
        export_path = Path(args.export)
        export_csv(display_records, export_path)


if __name__ == "__main__":
    main()
