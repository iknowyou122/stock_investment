"""Compare v2.2 vs v2.3 Triple Confirmation Engine performance.

Methodology:
  - v2.2 scores: loaded from historical scan_*.csv files (confidence column).
    These CSVs were produced by the v2.2 engine (breakout-confirmation logic).
  - v2.3 scores: the current TripleConfirmationEngine (pre-breakout compression logic),
    re-run against the same ticker/date pairs.
  - Outcomes: actual OHLCV T+2 / T+5 / T+10 return windows, fetched from FinMind.

Success criterion (inherited from Phase 4.15 T-2 strategy validation):
  Breakout defined as: max close in T+1 to T+10 >= twenty_day_high (as of signal date).
  Win/loss: breakout occurred within the measurement window.

Usage:
    python scripts/backtest_v23_vs_v22.py
    python scripts/backtest_v23_vs_v22.py --date 2026-04-01
    python scripts/backtest_v23_vs_v22.py --min-confidence 40
    python scripts/backtest_v23_vs_v22.py --sectors 1 2 3
    python scripts/backtest_v23_vs_v22.py --save-csv
    make backtest-compare
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    TimeRemainingColumn,
)
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.domain.triple_confirmation_engine import TripleConfirmationEngine
from taiwan_stock_agent.domain.models import (
    ChipReport,
    DailyOHLCV,
    VolumeProfile,
)
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)

_console = Console()
_lock = Lock()

_SCANS_DIR = Path(__file__).resolve().parents[1] / "data" / "scans"
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_WATCHLIST_CACHE_DIR = _DATA_DIR / "watchlist_cache"
_BACKTEST_DIR = _DATA_DIR / "backtest"

# Entry delay (T+2 from Phase 4.15 analysis)
_ENTRY_DELAY = 2

# Lookback window lengths used in VolumeProfile and breakout calculations
LOOKBACK_20 = 20
LOOKBACK_60 = 60

# Confidence tier buckets — used in distribution analysis and stratified tables
CONFIDENCE_TIERS = ["0-39", "40-49", "50-59", "60-69", "70-79", "80+"]
# Outcome window
_OUTCOME_DAYS = 10
# Breakout threshold: close must reach >= 20d_high * this factor
_BREAKOUT_THRESHOLD = 0.99

def _confidence_to_tier(score: int) -> str:
    """Map a confidence score to its display tier bucket string.

    Buckets align with CONFIDENCE_TIERS: 0-39, 40-49, 50-59, 60-69, 70-79, 80+.
    """
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


def _fmt_pct(v: float | None, green_above: float = 0.5) -> str:
    """Format a fraction as a colored percentage string for Rich output."""
    if v is None:
        return "[dim]—[/dim]"
    pct = v * 100
    color = "green" if v >= green_above else "red"
    return f"[{color}]{pct:.1f}%[/{color}]"


def _fmt_num(v: float | None, fmt: str = ".1f") -> str:
    """Format a number with a given format spec for Rich output."""
    if v is None:
        return "[dim]—[/dim]"
    return f"{v:{fmt}}"


CSV_FIELDS = [
    "signal_date",
    "ticker",
    "market",
    "industry",
    "v22_confidence",
    "v22_action",
    "v23_confidence",
    "v23_action",
    "entry_close",
    "twenty_day_high",
    "entry_delay_days",
    "win",
    "days_to_breakout",
    "max_return_pct",
    "final_return_pct",
    "v22_gate_pass",
    "v23_gate_pass",
]


# ---------------------------------------------------------------------------
# Load historical scan CSVs
# ---------------------------------------------------------------------------

def load_historical_signals(
    date_from: date | None = None,
    date_to: date | None = None,
    min_confidence: int = 0,
    sectors: list[int] | None = None,
) -> list[dict]:
    """Load signals from scan_*.csv files.

    Returns list of dicts with keys: signal_date, ticker, v22_confidence, v22_action.
    """
    # Load industry map for sector filtering
    industry_map = _load_industry_map()

    # Resolve sector names
    selected_industries: set[str] | None = None
    if sectors:
        selected_industries = _resolve_sector_names(industry_map, sectors)
        if selected_industries:
            _console.print(
                f"  [dim]產業篩選：{sorted(selected_industries)}[/dim]"
            )

    signals: list[dict] = []
    csv_files = sorted(_SCANS_DIR.glob("scan_*.csv"))

    if not csv_files:
        _console.print("[yellow]data/scans/ 中找不到 scan_*.csv 檔案。[/yellow]")
        return []

    for csv_path in csv_files:
        # Parse date from filename: scan_YYYY-MM-DD.csv
        try:
            file_date_str = csv_path.stem.replace("scan_", "")
            file_date = date.fromisoformat(file_date_str)
        except ValueError:
            continue

        # The scan_date in the CSV is the T+1 "entry" date; analysis_date is the signal date
        # Filter by analysis_date range
        if date_from or date_to:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                row_sample = next(reader, None)
                if row_sample is None:
                    continue
                # Use analysis_date field if present, else fall back to file_date
                analysis_date_str = row_sample.get("analysis_date") or row_sample.get("scan_date")
                if analysis_date_str:
                    try:
                        analysis_date_check = date.fromisoformat(analysis_date_str)
                        if date_from and analysis_date_check < date_from:
                            continue
                        if date_to and analysis_date_check > date_to:
                            continue
                    except ValueError:
                        pass

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    confidence = int(float(row.get("confidence", 0) or 0))
                except (ValueError, TypeError):
                    confidence = 0

                if confidence < min_confidence:
                    continue

                action = row.get("action", "CAUTION")
                if action == "CAUTION":
                    continue

                ticker = row.get("ticker", "").strip()
                if not ticker:
                    continue

                # Determine signal date (analysis_date is when the signal was computed)
                analysis_date_raw = row.get("analysis_date") or row.get("scan_date")
                try:
                    sig_date = date.fromisoformat(analysis_date_raw) if analysis_date_raw else file_date
                except ValueError:
                    sig_date = file_date

                if date_from and sig_date < date_from:
                    continue
                if date_to and sig_date > date_to:
                    continue

                # Sector filtering
                industry = industry_map.get(ticker, "未知")
                if selected_industries and industry not in selected_industries:
                    continue

                signals.append({
                    "signal_date": sig_date,
                    "ticker": ticker,
                    "v22_confidence": confidence,
                    "v22_action": action,
                    "industry": industry,
                })

    # Deduplicate by (ticker, signal_date): keep highest v22_confidence
    seen: dict[tuple, dict] = {}
    for sig in signals:
        key = (sig["ticker"], sig["signal_date"])
        if key not in seen or sig["v22_confidence"] > seen[key]["v22_confidence"]:
            seen[key] = sig

    result = sorted(seen.values(), key=lambda x: (x["signal_date"], x["ticker"]))
    _console.print(
        f"  [dim]載入 {len(result)} 個歷史信號（來自 {len(csv_files)} 個 CSV）[/dim]"
    )
    return result


def _load_industry_map() -> dict[str, str]:
    """Load the most recent cached industry map."""
    today = date.today()
    for delta in range(0, 14):
        candidate = today - timedelta(days=delta)
        path = _WATCHLIST_CACHE_DIR / f"industry_map_{candidate}.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
    # Try to build live
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from batch_plan import _build_industry_map  # type: ignore[import]
        return _build_industry_map() or {}
    except Exception:
        return {}


def _load_market_map() -> dict[str, str]:
    """Load the most recent cached market map (TSE/TPEx)."""
    today = date.today()
    for delta in range(0, 14):
        candidate = today - timedelta(days=delta)
        path = _WATCHLIST_CACHE_DIR / f"market_map_{candidate}.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
    return {}


def _resolve_sector_names(industry_map: dict[str, str], sector_indices: list[int]) -> set[str]:
    """Map 1-based sector index → industry name strings (sorted alphabetically)."""
    all_industries = sorted(set(industry_map.values()))
    idx_map = {i: name for i, name in enumerate(all_industries, start=1)}
    result: set[str] = set()
    for idx in sector_indices:
        if idx in idx_map:
            result.add(idx_map[idx])
        else:
            _console.print(f"  [yellow]警告：產業代號 {idx} 超出範圍，忽略[/yellow]")
    return result


# ---------------------------------------------------------------------------
# Re-score with v2.3 engine
# ---------------------------------------------------------------------------

def _df_to_ohlcv_list(df, ticker: str) -> list[DailyOHLCV]:
    bars = []
    for _, row in df.iterrows():
        bars.append(DailyOHLCV(
            ticker=ticker,
            trade_date=row["trade_date"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        ))
    return sorted(bars, key=lambda x: x.trade_date)


def _compute_volume_profile(
    history: list[DailyOHLCV],
    ticker: str,
    analysis_date: date,
) -> VolumeProfile:
    """Build a VolumeProfile from local history (no lookahead)."""
    # Only use bars up to analysis_date
    bars = sorted([b for b in history if b.trade_date <= analysis_date], key=lambda x: x.trade_date)

    twenty_day_high = max((b.high for b in bars[-LOOKBACK_20:]), default=0.0) if len(bars) >= 1 else 0.0
    sixty_day_high = max((b.high for b in bars[-LOOKBACK_60:]), default=0.0) if len(bars) >= 1 else 0.0

    # POC proxy: close on max-volume day in last 20 sessions
    last20 = bars[-LOOKBACK_20:] if bars else []
    poc_proxy = 0.0
    if last20:
        max_vol_bar = max(last20, key=lambda b: b.volume)
        poc_proxy = max_vol_bar.close

    return VolumeProfile(
        ticker=ticker,
        period_end=analysis_date,
        poc_proxy=poc_proxy,
        twenty_day_high=twenty_day_high,
        sixty_day_high=sixty_day_high,
        twenty_day_sessions=min(LOOKBACK_20, len(last20)),
        sixty_day_sessions=min(LOOKBACK_60, len(bars)),
        one_twenty_day_sessions=min(120, len(bars)),
        fiftytwo_week_sessions=min(250, len(bars)),
    )


def _empty_chip_report(ticker: str, sig_date: date) -> ChipReport:
    return ChipReport(
        ticker=ticker,
        report_date=sig_date,
        net_buyer_count_diff=0,
        active_branch_count=0,
        concentration_top15=0.0,
        top_buyers=[],
        risk_flags=[],
    )


def rescore_v23(
    ticker: str,
    signal_date: date,
    finmind: FinMindClient,
    chip_fetcher: ChipProxyFetcher,
    taiex_history: list[DailyOHLCV],
    market: str,
) -> dict | None:
    """Re-score ticker on signal_date using the current v2.3 engine.

    Returns a dict with keys: v23_confidence, v23_action, entry_close,
    twenty_day_high, v23_gate_pass, v23_flags.

    Returns None when:
    - OHLCV data is unavailable from FinMind (API error, 402, or empty response).
    - Fewer than 25 historical sessions exist up to signal_date (sparse data —
      Pillar 3 margin/SBL calculations require at least 25 bars).
    - An unexpected exception is raised during engine scoring.
    """
    try:
        ohlcv_start = signal_date - timedelta(days=130)
        df = finmind.fetch_ohlcv(ticker, start_date=ohlcv_start, end_date=signal_date)
        if df is None or df.empty:
            return None

        history = _df_to_ohlcv_list(df, ticker)
        # Must have at least 25 sessions for Pillar 3 calculations
        hist_up_to = [b for b in history if b.trade_date <= signal_date]
        if len(hist_up_to) < 25:
            return None

        today_bar = max(hist_up_to, key=lambda b: b.trade_date)
        ohlcv_history = [b for b in hist_up_to if b.trade_date < today_bar.trade_date]

        volume_profile = _compute_volume_profile(history, ticker, signal_date)

        # Free-tier chip data
        twse_proxy = None
        try:
            twse_proxy = chip_fetcher.fetch(ticker, signal_date)
        except Exception:
            pass

        chip_report = _empty_chip_report(ticker, signal_date)

        # Taiex history up to signal_date only (no lookahead)
        taiex_slice = [b for b in taiex_history if b.trade_date <= signal_date]

        eng = TripleConfirmationEngine()
        eng._taiex_history = taiex_slice
        eng._market = market

        signal_out, bd = eng.score_with_breakdown(
            ohlcv=today_bar,
            ohlcv_history=ohlcv_history,
            chip_report=chip_report,
            volume_profile=volume_profile,
            twse_proxy=twse_proxy,
            taiex_history=taiex_slice,
            market=market,
        )

        v23_gate_pass = "NO_SETUP" not in (signal_out.data_quality_flags or "")
        v23_action = signal_out.action
        v23_confidence = int(signal_out.confidence_score)

        return {
            "v23_confidence": v23_confidence,
            "v23_action": v23_action,
            "v23_gate_pass": v23_gate_pass,
            "v23_flags": bd.flags,
            "entry_close": today_bar.close,
            "twenty_day_high": volume_profile.twenty_day_high,
        }
    except Exception as e:
        logger.debug("rescore_v23 %s %s: %s", ticker, signal_date, e)
        return None


# ---------------------------------------------------------------------------
# Outcome evaluation
# ---------------------------------------------------------------------------

def fetch_future_bars(
    ticker: str,
    signal_date: date,
    finmind: FinMindClient,
    entry_delay: int = _ENTRY_DELAY,
    window: int = _OUTCOME_DAYS,
) -> list[DailyOHLCV]:
    """Fetch future bars T+entry_delay to T+entry_delay+window."""
    start = signal_date + timedelta(days=1)
    end = signal_date + timedelta(days=entry_delay + window + 5)
    try:
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        bars = _df_to_ohlcv_list(df, ticker)
        return [b for b in bars if b.trade_date > signal_date]
    except Exception:
        return []


def check_outcome(
    entry_close: float,
    twenty_day_high: float,
    future_bars: list[DailyOHLCV],
    entry_delay: int = _ENTRY_DELAY,
    breakout_threshold: float = _BREAKOUT_THRESHOLD,
) -> dict:
    """Evaluate trade outcome.

    Returns:
        win: True if price broke above twenty_day_high within window
        days_to_breakout: first day breakout occurred (1-based), or 0
        max_return_pct: max close / entry_close - 1 across window
        final_return_pct: last close / entry_close - 1
    """
    if not future_bars or entry_delay >= len(future_bars):
        return {
            "win": False,
            "days_to_breakout": 0,
            "max_return_pct": 0.0,
            "final_return_pct": 0.0,
        }

    # Entry is at close of bar at index entry_delay (0-based)
    entry_bar = future_bars[entry_delay - 1] if entry_delay > 0 else None
    entry_price = entry_bar.close if entry_bar else entry_close

    if entry_price <= 0:
        entry_price = entry_close

    # Outcome window: bars after entry
    outcome_bars = future_bars[entry_delay:]

    if not outcome_bars:
        return {
            "win": False,
            "days_to_breakout": 0,
            "max_return_pct": 0.0,
            "final_return_pct": 0.0,
        }

    # 0.99 slippage tolerance: a close within 1% of the 20d high is counted as
    # a successful breakout. This avoids penalising signals where the close
    # lands fractionally below the high due to intraday spread or rounding.
    # See signal-engine-design.md §"Breakout success criterion".
    breakout_target = twenty_day_high * breakout_threshold

    days_to_breakout = 0
    win = False
    for i, bar in enumerate(outcome_bars, start=1):
        if bar.close >= breakout_target:
            win = True
            days_to_breakout = i
            break

    max_close = max(b.close for b in outcome_bars)
    final_close = outcome_bars[-1].close

    max_return_pct = (max_close - entry_price) / entry_price if entry_price > 0 else 0.0
    final_return_pct = (final_close - entry_price) / entry_price if entry_price > 0 else 0.0

    return {
        "win": win,
        "days_to_breakout": days_to_breakout,
        "max_return_pct": max_return_pct,
        "final_return_pct": final_return_pct,
    }


# ---------------------------------------------------------------------------
# Metric calculation helpers
# ---------------------------------------------------------------------------

def compute_engine_metrics(records: list[dict], engine: str) -> dict:
    """Compute aggregate metrics for one engine version.

    Args:
        records: list of result dicts with win, max_return_pct, days_to_breakout,
            v22_action / v23_action, v22_confidence / v23_confidence
        engine: "v22" or "v23"

    Returns:
        dict with n, win_rate, false_rate, avg_upside, avg_days_to_breakout,
        avg_confidence, pct_ready (LONG/READY signals), pct_watch
    """
    conf_key = f"{engine}_confidence"
    action_key = f"{engine}_action"
    gate_key = f"{engine}_gate_pass"

    # For v2.2 engine: any record loaded from CSV is gate-passed (it appeared in output)
    if engine == "v22":
        active = [r for r in records if r.get("v22_action") in ("LONG", "WATCH")]
    else:
        active = [r for r in records if r.get("v23_gate_pass", False)]

    n = len(active)
    if n == 0:
        return {
            "n": 0,
            "win_rate": None,
            "false_rate": None,
            "avg_upside": None,
            "avg_days_to_breakout": None,
            "avg_confidence": None,
            "pct_long": None,
            "pct_watch": None,
        }

    wins = [r for r in active if r["win"]]
    win_rate = len(wins) / n

    long_signals = [r for r in active if r.get(action_key) == "LONG"]
    watch_signals = [r for r in active if r.get(action_key) == "WATCH"]

    returns = [r["max_return_pct"] for r in active]
    avg_upside = sum(returns) / n if returns else 0.0

    days_list = [r["days_to_breakout"] for r in wins if r["days_to_breakout"] > 0]
    avg_days = sum(days_list) / len(days_list) if days_list else 0.0

    confidences = [r.get(conf_key, 0) for r in active]
    avg_conf = sum(confidences) / n if confidences else 0.0

    return {
        "n": n,
        "win_rate": win_rate,
        "false_rate": 1.0 - win_rate,
        "avg_upside": avg_upside,
        "avg_days_to_breakout": avg_days,
        "avg_confidence": avg_conf,
        "pct_long": len(long_signals) / n if n > 0 else 0.0,
        "pct_watch": len(watch_signals) / n if n > 0 else 0.0,
    }


def compute_confidence_distribution(records: list[dict], engine: str) -> dict[str, int]:
    """Bucket records by confidence tier for one engine."""
    conf_key = f"{engine}_confidence"
    tiers: dict[str, int] = {t: 0 for t in CONFIDENCE_TIERS}
    for r in records:
        c = int(r.get(conf_key, 0) or 0)
        tiers[_confidence_to_tier(c)] += 1
    return tiers


# ---------------------------------------------------------------------------
# Rich output
# ---------------------------------------------------------------------------

def print_comparison_table(records: list[dict]) -> None:
    """Print side-by-side v2.2 vs v2.3 engine comparison."""
    v22 = compute_engine_metrics(records, "v22")
    v23 = compute_engine_metrics(records, "v23")

    _console.rule("[bold cyan]v2.2 vs v2.3 引擎對比[/bold cyan]")
    _console.print(f"  [dim]信號總數（歷史 CSV 載入）：{len(records)}[/dim]\n")

    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        border_style="bright_black",
        title="[bold cyan]引擎效能對比[/bold cyan]",
    )
    tbl.add_column("指標", style="white", width=20)
    tbl.add_column("v2.2（突破確認）", justify="right", width=18)
    tbl.add_column("v2.3（蓄積偵測）", justify="right", width=18)
    tbl.add_column("差異", justify="right", width=14)

    def _delta(v22_val: float | None, v23_val: float | None, fmt: str = ".1f", pct: bool = False) -> str:
        if v22_val is None or v23_val is None:
            return "[dim]—[/dim]"
        delta = v23_val - v22_val
        scale = 100 if pct else 1
        color = "green" if delta > 0 else ("red" if delta < 0 else "dim")
        prefix = "+" if delta > 0 else ""
        return f"[{color}]{prefix}{delta * scale:{fmt}}{'%' if pct else ''}[/{color}]"

    rows = [
        ("有效信號數", str(v22["n"]), str(v23["n"]),
         _delta(v22["n"], v23["n"], fmt=".0f")),

        ("勝率（突破率）",
         _fmt_pct(v22["win_rate"]),
         _fmt_pct(v23["win_rate"]),
         _delta(v22["win_rate"], v23["win_rate"], fmt=".1f", pct=True)),

        ("假信號率",
         _fmt_pct(v22["false_rate"], green_above=0.0),
         _fmt_pct(v23["false_rate"], green_above=0.0),
         _delta(v23["false_rate"], v22["false_rate"], fmt=".1f", pct=True)),

        ("平均最大報酬",
         f"{(v22['avg_upside'] or 0) * 100:.2f}%",
         f"{(v23['avg_upside'] or 0) * 100:.2f}%",
         _delta(v22["avg_upside"], v23["avg_upside"], fmt=".2f", pct=True)),

        ("平均突破天數",
         _fmt_num(v22["avg_days_to_breakout"]),
         _fmt_num(v23["avg_days_to_breakout"]),
         _delta(v22["avg_days_to_breakout"], v23["avg_days_to_breakout"], fmt=".1f")),

        ("平均信心分數",
         _fmt_num(v22["avg_confidence"], fmt=".1f"),
         _fmt_num(v23["avg_confidence"], fmt=".1f"),
         _delta(v22["avg_confidence"], v23["avg_confidence"], fmt=".1f")),

        ("LONG 佔比",
         _fmt_pct(v22["pct_long"], green_above=0.3),
         _fmt_pct(v23["pct_long"], green_above=0.3),
         _delta(v22["pct_long"], v23["pct_long"], fmt=".1f", pct=True)),

        ("WATCH 佔比",
         _fmt_pct(v22["pct_watch"], green_above=0.2),
         _fmt_pct(v23["pct_watch"], green_above=0.2),
         _delta(v22["pct_watch"], v23["pct_watch"], fmt=".1f", pct=True)),
    ]

    for row in rows:
        tbl.add_row(*row)

    _console.print(tbl)


def print_stratified_tables(records: list[dict], industry_map: dict[str, str], market_map: dict[str, str]) -> None:
    """Print stratified metrics by industry and market."""
    _console.print()

    # --- By market (TSE vs TPEx) ---
    by_market: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        mkt = market_map.get(r["ticker"], "TSE")
        by_market[mkt].append(r)

    mkt_tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold yellow",
        border_style="bright_black",
        title="[yellow]按市場別分層[/yellow]",
    )
    mkt_tbl.add_column("市場", width=8)
    mkt_tbl.add_column("N", justify="right", width=6)
    mkt_tbl.add_column("v2.2 勝率", justify="right", width=10)
    mkt_tbl.add_column("v2.3 勝率", justify="right", width=10)
    mkt_tbl.add_column("v2.2 均報酬", justify="right", width=11)
    mkt_tbl.add_column("v2.3 均報酬", justify="right", width=11)

    for mkt in sorted(by_market.keys()):
        rows = by_market[mkt]
        m22 = compute_engine_metrics(rows, "v22")
        m23 = compute_engine_metrics(rows, "v23")
        wr22 = f"{(m22['win_rate'] or 0) * 100:.1f}%" if m22["win_rate"] is not None else "—"
        wr23 = f"{(m23['win_rate'] or 0) * 100:.1f}%" if m23["win_rate"] is not None else "—"
        ret22 = f"{(m22['avg_upside'] or 0) * 100:+.2f}%" if m22["avg_upside"] is not None else "—"
        ret23 = f"{(m23['avg_upside'] or 0) * 100:+.2f}%" if m23["avg_upside"] is not None else "—"
        mkt_tbl.add_row(mkt, str(len(rows)), wr22, wr23, ret22, ret23)

    _console.print(mkt_tbl)
    _console.print()

    # --- By industry (top 10 by signal count) ---
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        ind = r.get("industry", "未知")
        by_industry[ind].append(r)

    # Sort by signal count descending, show top 10
    top_industries = sorted(by_industry.items(), key=lambda x: len(x[1]), reverse=True)[:10]

    ind_tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold magenta",
        border_style="bright_black",
        title="[magenta]按產業分層（前 10）[/magenta]",
    )
    ind_tbl.add_column("產業", width=18)
    ind_tbl.add_column("N", justify="right", width=6)
    ind_tbl.add_column("v2.2 勝率", justify="right", width=10)
    ind_tbl.add_column("v2.3 勝率", justify="right", width=10)
    ind_tbl.add_column("v2.3 均信心", justify="right", width=11)

    for ind, rows in top_industries:
        m22 = compute_engine_metrics(rows, "v22")
        m23 = compute_engine_metrics(rows, "v23")
        wr22 = f"{(m22['win_rate'] or 0) * 100:.1f}%" if m22["win_rate"] is not None else "—"
        wr23 = f"{(m23['win_rate'] or 0) * 100:.1f}%" if m23["win_rate"] is not None else "—"
        conf23 = f"{m23['avg_confidence']:.0f}" if m23["avg_confidence"] is not None else "—"
        ind_tbl.add_row(ind[:17], str(len(rows)), wr22, wr23, conf23)

    _console.print(ind_tbl)
    _console.print()

    # --- By confidence tier (v2.3) ---
    tier_map: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        c = int(r.get("v23_confidence", 0) or 0)
        tier_map[_confidence_to_tier(c)].append(r)

    tier_tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        title="[cyan]v2.3 信心分層勝率[/cyan]",
    )
    tier_tbl.add_column("信心區間", width=12)
    tier_tbl.add_column("N", justify="right", width=6)
    tier_tbl.add_column("勝率", justify="right", width=10)
    tier_tbl.add_column("均最大報酬", justify="right", width=12)
    tier_tbl.add_column("均突破天數", justify="right", width=11)

    for tier in CONFIDENCE_TIERS:
        rows = tier_map.get(tier, [])
        if not rows:
            tier_tbl.add_row(tier, "0", "[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]")
            continue
        n = len(rows)
        wins = [r for r in rows if r["win"]]
        wr = len(wins) / n
        returns = [r["max_return_pct"] for r in rows]
        avg_ret = sum(returns) / n if returns else 0.0
        days_list = [r["days_to_breakout"] for r in wins if r["days_to_breakout"] > 0]
        avg_days = sum(days_list) / len(days_list) if days_list else 0.0

        wr_color = "green" if wr >= 0.5 else "red"
        tier_tbl.add_row(
            tier,
            str(n),
            f"[{wr_color}]{wr * 100:.1f}%[/{wr_color}]",
            f"[{'green' if avg_ret >= 0 else 'red'}]{avg_ret * 100:+.2f}%[/{'green' if avg_ret >= 0 else 'red'}]",
            f"{avg_days:.1f}" if avg_days > 0 else "[dim]—[/dim]",
        )

    _console.print(tier_tbl)


def print_top_signals(records: list[dict], top_n: int = 15) -> None:
    """Print top signals where v2.3 scores higher than v2.2."""
    # Signals that v2.3 upgrades (gap > 5 pts)
    upgraded = [
        r for r in records
        if (r.get("v23_confidence") or 0) > (r.get("v22_confidence") or 0) + 5
        and r.get("v23_gate_pass", False)
    ]
    upgraded.sort(key=lambda x: (x.get("v23_confidence") or 0), reverse=True)

    if not upgraded:
        return

    _console.print()
    _console.rule(f"[bold]v2.3 升分前 {min(top_n, len(upgraded))} 名（v2.3 > v2.2 + 5分）[/bold]")

    top_tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold",
        border_style="bright_black",
    )
    top_tbl.add_column("日期", width=12)
    top_tbl.add_column("代號", width=8)
    top_tbl.add_column("產業", width=14)
    top_tbl.add_column("v2.2", justify="right", width=6)
    top_tbl.add_column("v2.3", justify="right", width=6)
    top_tbl.add_column("結果", width=6)
    top_tbl.add_column("最大報酬", justify="right", width=10)

    for r in upgraded[:top_n]:
        success_str = "[green]✓[/green]" if r["win"] else "[red]✗[/red]"
        ret = r["max_return_pct"] * 100
        top_tbl.add_row(
            str(r["signal_date"]),
            r["ticker"],
            (r.get("industry") or "未知")[:13],
            str(r.get("v22_confidence") or 0),
            str(r.get("v23_confidence") or 0),
            success_str,
            f"[{'green' if ret >= 0 else 'red'}]{ret:+.2f}%[/{'green' if ret >= 0 else 'red'}]",
        )

    _console.print(top_tbl)


# ---------------------------------------------------------------------------
# Save CSV
# ---------------------------------------------------------------------------

def save_csv(records: list[dict], out_path: Path) -> None:
    """Save raw comparison data to CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(records, key=lambda x: (x["signal_date"], x["ticker"])):
            row = {
                "signal_date": r["signal_date"].isoformat() if isinstance(r["signal_date"], date) else r["signal_date"],
                "ticker": r["ticker"],
                "market": r.get("market", ""),
                "industry": r.get("industry", ""),
                "v22_confidence": r.get("v22_confidence", 0),
                "v22_action": r.get("v22_action", ""),
                "v23_confidence": r.get("v23_confidence", 0),
                "v23_action": r.get("v23_action", ""),
                "entry_close": r.get("entry_close", 0),
                "twenty_day_high": r.get("twenty_day_high", 0),
                "entry_delay_days": _ENTRY_DELAY,
                "win": r.get("win", False),
                "days_to_breakout": r.get("days_to_breakout", 0),
                "max_return_pct": round(r.get("max_return_pct", 0), 6),
                "final_return_pct": round(r.get("final_return_pct", 0), 6),
                "v22_gate_pass": True,  # by definition — it was in the scan output
                "v23_gate_pass": r.get("v23_gate_pass", False),
            }
            writer.writerow(row)
    _console.print(f"\n  [green]CSV 已儲存：[/green]{out_path}  ({len(records)} 筆)")


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_comparison(
    date_from: date | None,
    date_to: date | None,
    min_confidence: int,
    sectors: list[int] | None,
    workers: int,
    save_csv_output: bool,
) -> list[dict]:
    """Full comparison pipeline. Returns list of result records."""

    _console.print()
    _console.print(Panel(
        "[bold white]v2.2 vs v2.3 信號引擎回測比對[/bold white]\n"
        "[dim]v2.2 = 突破確認（scan CSV 歷史分數）\n"
        "v2.3 = 蓄積偵測（當前引擎重新計分）[/dim]",
        title="[bold cyan]Backtest Compare[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # --- Load historical signals ---
    _console.print("\n[bold cyan]Phase 1/4[/bold cyan] 載入歷史信號...")
    signals = load_historical_signals(
        date_from=date_from,
        date_to=date_to,
        min_confidence=min_confidence,
        sectors=sectors,
    )
    if not signals:
        _console.print("[red]未找到歷史信號，請確認 data/scans/ 中有 scan_*.csv 檔案。[/red]")
        return []

    # Load auxiliary maps
    industry_map = _load_industry_map()
    market_map = _load_market_map()

    # --- Pre-fetch TAIEX history ---
    _console.print("\n[bold cyan]Phase 2/4[/bold cyan] 預載 TAIEX 歷史...")
    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

    earliest_date = min(s["signal_date"] for s in signals)
    latest_date = max(s["signal_date"] for s in signals)
    taiex_lookback = (date.today() - earliest_date).days + 30

    taiex_history: list[DailyOHLCV] = []
    try:
        taiex_df = finmind.fetch_taiex_history(latest_date, lookback_days=taiex_lookback)
        if taiex_df is not None and not taiex_df.empty:
            for _, row in taiex_df.iterrows():
                taiex_history.append(DailyOHLCV(
                    ticker="TAIEX",
                    trade_date=row["trade_date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume", 0)),
                ))
        taiex_history = sorted(taiex_history, key=lambda x: x.trade_date)
        _console.print(f"  TAIEX 歷史 [bold]{len(taiex_history)}[/bold] 筆")
    except Exception as e:
        _console.print(f"  [yellow]TAIEX 載入失敗（繼續執行）：{e}[/yellow]")

    # --- Phase 3: re-score with v2.3 ---
    _console.print(f"\n[bold cyan]Phase 3/4[/bold cyan] v2.3 重新計分 ({len(signals)} 信號)...")

    v23_results: list[dict] = []

    def _rescore_worker(sig: dict) -> dict:
        time.sleep(0.1)
        ticker = sig["ticker"]
        sig_date = sig["signal_date"]
        market = market_map.get(ticker, "TSE")
        result = rescore_v23(ticker, sig_date, finmind, chip_fetcher, taiex_history, market)
        merged = {**sig, "market": market}
        if result is not None:
            merged.update(result)
        else:
            # Could not re-score — mark as gate-failed v2.3
            merged["v23_confidence"] = 0
            merged["v23_action"] = "CAUTION"
            merged["v23_gate_pass"] = False
            merged["entry_close"] = 0.0
            merged["twenty_day_high"] = 0.0
        return merged

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("v2.3 計分...", total=len(signals))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_rescore_worker, sig): sig for sig in signals}
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    r = future.result()
                    with _lock:
                        v23_results.append(r)
                except Exception as e:
                    logger.debug("rescore worker error: %s", e)

    _console.print(f"  v2.3 計分完成：[bold]{len(v23_results)}[/bold] 筆")

    # --- Phase 4: fetch outcomes ---
    _console.print(f"\n[bold cyan]Phase 4/4[/bold cyan] 取得交易結果 ({len(v23_results)} 信號)...")

    final_results: list[dict] = []

    def _outcome_worker(sig: dict) -> dict:
        time.sleep(0.05)
        entry_close = sig.get("entry_close", 0.0) or 0.0
        twenty_day_high = sig.get("twenty_day_high", 0.0) or 0.0

        if entry_close <= 0 or twenty_day_high <= 0:
            return {**sig, "win": False, "days_to_breakout": 0,
                    "max_return_pct": 0.0, "final_return_pct": 0.0}

        future_bars = fetch_future_bars(sig["ticker"], sig["signal_date"], finmind)
        outcome = check_outcome(entry_close, twenty_day_high, future_bars)
        return {**sig, **outcome}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("取得結果...", total=len(v23_results))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_outcome_worker, sig): sig for sig in v23_results}
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    r = future.result()
                    with _lock:
                        final_results.append(r)
                except Exception as e:
                    logger.debug("outcome worker error: %s", e)

    # Filter out records with no entry data (data unavailable)
    final_results = [r for r in final_results if (r.get("entry_close") or 0) > 0]
    _console.print(f"  結果取得完成：[bold]{len(final_results)}[/bold] 筆有效記錄")

    if not final_results:
        _console.print("[yellow]無有效記錄可供分析。[/yellow]")
        return []

    # --- Output ---
    print_comparison_table(final_results)
    print_stratified_tables(final_results, industry_map, market_map)
    print_top_signals(final_results)

    if save_csv_output:
        out_path = _BACKTEST_DIR / f"backtest_compare_{date.today().isoformat()}.csv"
        save_csv(final_results, out_path)

    return final_results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="v2.2 vs v2.3 信號引擎回測比對"
    )
    parser.add_argument(
        "--date-from",
        type=date.fromisoformat,
        default=None,
        help="回測起始日 YYYY-MM-DD（預設：所有可用 CSV）",
    )
    parser.add_argument(
        "--date-to",
        type=date.fromisoformat,
        default=None,
        help="回測結束日 YYYY-MM-DD（預設：10天前，保留 T+10 結果窗口）",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="快捷：只分析特定日期（等同 --date-from X --date-to X）",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=40,
        help="最低信心分數門檻（預設：40）",
    )
    parser.add_argument(
        "--sectors",
        nargs="+",
        type=int,
        default=None,
        metavar="N",
        help="產業代號篩選（空白分隔，同 batch_scan 選單）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="並行 worker 數（預設：4）",
    )
    parser.add_argument(
        "--save-csv",
        action="store_true",
        help="儲存原始比對資料 CSV",
    )
    args = parser.parse_args()

    # Resolve date range
    date_from = args.date_from
    date_to = args.date_to

    if args.date:
        date_from = args.date
        date_to = args.date

    # Default date_to: 10 days ago to ensure T+10 window is available
    if date_to is None:
        date_to = date.today() - timedelta(days=10)

    if date_from and date_to and date_from > date_to:
        _console.print("[red]錯誤：date-from 必須早於或等於 date-to[/red]")
        sys.exit(1)

    if date_to and (date.today() - date_to).days < 10:
        _console.print(
            "[yellow]警告：date-to 距今不足 10 天，T+10 結果窗口可能不完整[/yellow]"
        )

    run_comparison(
        date_from=date_from,
        date_to=date_to,
        min_confidence=args.min_confidence,
        sectors=args.sectors,
        workers=args.workers,
        save_csv_output=args.save_csv,
    )


if __name__ == "__main__":
    main()
