"""Coil signal outcome tracker.

Ingests daily coil_*.csv snapshots into a local SQLite database, checks each
pending signal against future OHLCV bars, and computes rolling win-rates by
grade (COIL_PRIME / COIL_MATURE / COIL_EARLY).

Usage:
    python scripts/coil_monitor.py                       # Dashboard (last 30 days)
    python scripts/coil_monitor.py --date 2026-04-15     # Stats for a specific date
    python scripts/coil_monitor.py --grade COIL_PRIME    # Filter by grade
    python scripts/coil_monitor.py --top 10              # Top performers
    python scripts/coil_monitor.py --export report.csv   # Export to CSV
    python scripts/coil_monitor.py --refresh             # Force re-check all PENDING
    make coil-monitor
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Generator, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

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
_DB_PATH = _ROOT / "db" / "coil_track.db"
_SCANS_DIR = _ROOT / "data" / "scans"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BREAKOUT_THRESHOLD = 1.01   # close > resistance × 1.01
TRACKING_WINDOW = 10         # trading days to watch
EXPIRY_WINDOW = 15           # days after which PENDING → EXPIRED
MIN_SAMPLE_FOR_WINRATE = 20  # per grade — suppress display below this

GRADE_ORDER = ["COIL_PRIME", "COIL_MATURE", "COIL_EARLY"]
GRADE_COLOR = {
    "COIL_PRIME": "bold magenta",
    "COIL_MATURE": "bold cyan",
    "COIL_EARLY": "yellow",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CoilSignalRecord:
    sig_id: str                              # {ticker}_{analysis_date}_{grade}
    ticker: str
    name: str
    market: str
    signal_date: str                         # ISO date string
    grade: str
    score: int
    bb_pct: float
    vol_ratio: float
    weeks_consolidating: int
    vs_60d_high_pct: float
    entry_close: float
    resistance: float                        # pinned at ingest — never re-derived
    outcome: str = "PENDING"
    days_to_breakout: Optional[int] = None
    max_gain_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None
    checked_thru: Optional[str] = None
    breakout_date: Optional[str] = None


# ---------------------------------------------------------------------------
# Resistance derivation (pure — testable)
# ---------------------------------------------------------------------------

def _derive_resistance(entry_close: float, vs_60d_high_pct: float) -> float:
    """Derive 60d high resistance from close and vs_60d_high_pct.

    vs_60d_high_pct = (close / 60d_high - 1) × 100, e.g. -10.12
    Therefore: 60d_high = close / (1 + vs_60d_high_pct / 100)

    Example: entry_close=89.0, vs_60d_high_pct=-10.12 → 89.0 / 0.8988 ≈ 99.02
    """
    denominator = 1.0 + vs_60d_high_pct / 100.0
    if abs(denominator) < 1e-9:
        return entry_close
    return entry_close / denominator


# ---------------------------------------------------------------------------
# Outcome evaluation (pure — testable)
# ---------------------------------------------------------------------------

def _evaluate_coil_outcome(
    entry_close: float,
    resistance: float,
    future_bars: list[dict],
) -> tuple[str, Optional[int], Optional[float], Optional[float]]:
    """Return (outcome, days_to_breakout, max_gain_pct, mae_pct).

    outcome: PENDING | BREAKOUT | STALLED | EXPIRED
    mae_pct uses bar low (not close) to capture worst intraday price.
    """
    if not future_bars:
        return "PENDING", None, None, None

    threshold = resistance * BREAKOUT_THRESHOLD
    min_low = entry_close      # track drawdown via bar low
    max_close = entry_close

    for i, bar in enumerate(future_bars, start=1):
        min_low = min(min_low, float(bar.get("low", entry_close)))
        max_close = max(max_close, float(bar["close"]))
        if float(bar["close"]) >= threshold:
            mae = (min_low / entry_close - 1) * 100
            gain = (max_close / entry_close - 1) * 100
            return "BREAKOUT", i, gain, mae

    mae = (min_low / entry_close - 1) * 100
    gain = (max_close / entry_close - 1) * 100
    if len(future_bars) >= EXPIRY_WINDOW:
        return "EXPIRED", None, gain, mae
    return "STALLED", None, gain, mae


# ---------------------------------------------------------------------------
# Win-rate display guard (pure — testable)
# ---------------------------------------------------------------------------

def _format_winrate(n: int, wins: int) -> str:
    if n < MIN_SAMPLE_FOR_WINRATE:
        return f"[dim]N={n} (不足 {MIN_SAMPLE_FOR_WINRATE} 筆)[/dim]"
    pct = wins / n * 100
    color = "green" if pct >= 55 else ("yellow" if pct >= 45 else "red")
    return f"[{color}]{pct:.0f}%[/{color}]  [dim](N={n})[/dim]"


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

@contextmanager
def _get_conn(db_path: Path = _DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(db_path: Path = _DB_PATH) -> None:
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS coil_signals (
                sig_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                name TEXT DEFAULT '',
                market TEXT DEFAULT 'TSE',
                signal_date TEXT NOT NULL,
                grade TEXT NOT NULL,
                score INTEGER DEFAULT 0,
                bb_pct REAL DEFAULT 0.0,
                vol_ratio REAL DEFAULT 0.0,
                weeks_consolidating INTEGER DEFAULT 0,
                vs_60d_high_pct REAL DEFAULT 0.0,
                entry_close REAL NOT NULL,
                resistance REAL NOT NULL,
                outcome TEXT DEFAULT 'PENDING',
                days_to_breakout INTEGER,
                max_gain_pct REAL,
                max_adverse_excursion_pct REAL,
                checked_thru TEXT,
                breakout_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_coil_outcome ON coil_signals (outcome);
            CREATE INDEX IF NOT EXISTS idx_coil_grade   ON coil_signals (grade);
            CREATE INDEX IF NOT EXISTS idx_coil_date    ON coil_signals (signal_date);
        """)


def _upsert_signal(rec: CoilSignalRecord, db_path: Path = _DB_PATH) -> None:
    with _get_conn(db_path) as conn:
        conn.execute("""
            INSERT INTO coil_signals
                (sig_id, ticker, name, market, signal_date, grade, score,
                 bb_pct, vol_ratio, weeks_consolidating, vs_60d_high_pct,
                 entry_close, resistance, outcome, days_to_breakout,
                 max_gain_pct, max_adverse_excursion_pct, checked_thru,
                 breakout_date, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(sig_id) DO UPDATE SET
                outcome                   = excluded.outcome,
                days_to_breakout          = excluded.days_to_breakout,
                max_gain_pct              = excluded.max_gain_pct,
                max_adverse_excursion_pct = excluded.max_adverse_excursion_pct,
                checked_thru              = excluded.checked_thru,
                breakout_date             = excluded.breakout_date,
                updated_at                = datetime('now')
        """, (
            rec.sig_id, rec.ticker, rec.name, rec.market, rec.signal_date,
            rec.grade, rec.score, rec.bb_pct, rec.vol_ratio,
            rec.weeks_consolidating, rec.vs_60d_high_pct,
            rec.entry_close, rec.resistance, rec.outcome,
            rec.days_to_breakout, rec.max_gain_pct,
            rec.max_adverse_excursion_pct, rec.checked_thru,
            rec.breakout_date,
        ))


def _load_all(db_path: Path = _DB_PATH) -> list[CoilSignalRecord]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM coil_signals ORDER BY signal_date DESC").fetchall()
    return [_row_to_record(r) for r in rows]


def _load_pending(db_path: Path = _DB_PATH) -> list[CoilSignalRecord]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coil_signals WHERE outcome = 'PENDING' ORDER BY signal_date"
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def _row_to_record(row: sqlite3.Row) -> CoilSignalRecord:
    return CoilSignalRecord(
        sig_id=row["sig_id"],
        ticker=row["ticker"],
        name=row["name"] or "",
        market=row["market"] or "TSE",
        signal_date=row["signal_date"],
        grade=row["grade"],
        score=row["score"] or 0,
        bb_pct=row["bb_pct"] or 0.0,
        vol_ratio=row["vol_ratio"] or 0.0,
        weeks_consolidating=row["weeks_consolidating"] or 0,
        vs_60d_high_pct=row["vs_60d_high_pct"] or 0.0,
        entry_close=row["entry_close"],
        resistance=row["resistance"],
        outcome=row["outcome"] or "PENDING",
        days_to_breakout=row["days_to_breakout"],
        max_gain_pct=row["max_gain_pct"],
        max_adverse_excursion_pct=row["max_adverse_excursion_pct"],
        checked_thru=row["checked_thru"],
        breakout_date=row["breakout_date"],
    )


# ---------------------------------------------------------------------------
# CSV ingestion
# ---------------------------------------------------------------------------

def ingest_coil_csv(csv_path: Path, db_path: Path = _DB_PATH) -> int:
    """Read a coil_*.csv and insert new signals as PENDING. Returns count inserted."""
    if not csv_path.exists():
        return 0
    _ensure_schema(db_path)
    inserted = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = row.get("ticker", "").strip()
            grade = row.get("grade", "").strip()
            analysis_date = row.get("analysis_date", "").strip()
            if not ticker or not grade or not analysis_date:
                continue
            try:
                entry_close = float(row.get("score", 0))  # placeholder — will fetch below
            except (ValueError, TypeError):
                entry_close = 0.0

            try:
                vs_60d_high_pct = float(row.get("vs_60d_high_pct", 0))
            except (ValueError, TypeError):
                vs_60d_high_pct = 0.0

            sig_id = f"{ticker}_{analysis_date}_{grade}"

            # Check if already exists
            with _get_conn(db_path) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM coil_signals WHERE sig_id = ?", (sig_id,)
                ).fetchone()
            if exists:
                continue

            # entry_close needs to be fetched from OHLCV; store 0.0 as placeholder
            # resistance will be properly set by refresh after entry_close is known
            try:
                score = int(float(row.get("score", 0)))
            except (ValueError, TypeError):
                score = 0
            try:
                bb_pct = float(row.get("bb_pct", 0))
            except (ValueError, TypeError):
                bb_pct = 0.0
            try:
                vol_ratio = float(row.get("vol_ratio", 0))
            except (ValueError, TypeError):
                vol_ratio = 0.0
            try:
                weeks = int(float(row.get("weeks_consolidating", 0)))
            except (ValueError, TypeError):
                weeks = 0

            rec = CoilSignalRecord(
                sig_id=sig_id,
                ticker=ticker,
                name=row.get("name", ""),
                market=row.get("market", "TSE"),
                signal_date=analysis_date,
                grade=grade,
                score=score,
                bb_pct=bb_pct,
                vol_ratio=vol_ratio,
                weeks_consolidating=weeks,
                vs_60d_high_pct=vs_60d_high_pct,
                entry_close=0.0,       # filled during refresh
                resistance=0.0,        # filled during refresh
                outcome="PENDING",
            )
            _upsert_signal(rec, db_path)
            inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# OHLCV helpers
# ---------------------------------------------------------------------------

def _fetch_bars(
    ticker: str,
    start: date,
    end: date,
    finmind: FinMindClient,
) -> list[dict]:
    try:
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        bars = []
        for _, row in df.iterrows():
            bars.append({
                "trade_date": row["trade_date"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            })
        return sorted(bars, key=lambda x: x["trade_date"])
    except Exception as e:
        logger.debug("_fetch_bars %s: %s", ticker, e)
        return []


# ---------------------------------------------------------------------------
# Refresh (check outcomes for PENDING signals)
# ---------------------------------------------------------------------------

def refresh_outcomes(
    db_path: Path = _DB_PATH,
    finmind: Optional[FinMindClient] = None,
    workers: int = 4,
    force: bool = False,
) -> int:
    """Check all PENDING signals against OHLCV. Returns count updated."""
    _ensure_schema(db_path)

    if finmind is None:
        finmind = FinMindClient()

    pending = _load_pending(db_path)
    if not pending:
        _console.print("  [dim]沒有待確認信號。[/dim]")
        return 0

    today = date.today()
    updated = 0
    update_lock = Lock()

    def _process(rec: CoilSignalRecord) -> Optional[CoilSignalRecord]:
        time.sleep(0.1)
        try:
            sig_date = date.fromisoformat(rec.signal_date)
        except ValueError:
            return None

        # Fetch the signal-date bar to get entry_close if missing
        entry_close = rec.entry_close
        if entry_close <= 0:
            bars_before = _fetch_bars(
                rec.ticker,
                sig_date - timedelta(days=10),
                sig_date,
                finmind,
            )
            bars_on_date = [b for b in bars_before if b["trade_date"] <= sig_date]
            if not bars_on_date:
                return None
            entry_close = bars_on_date[-1]["close"]
            rec.entry_close = entry_close
            rec.resistance = _derive_resistance(entry_close, rec.vs_60d_high_pct)

        if rec.resistance <= 0 and rec.vs_60d_high_pct != 0:
            rec.resistance = _derive_resistance(entry_close, rec.vs_60d_high_pct)

        # Fetch future bars
        future_bars = _fetch_bars(
            rec.ticker,
            sig_date + timedelta(days=1),
            sig_date + timedelta(days=EXPIRY_WINDOW + 10),
            finmind,
        )
        future_bars = [b for b in future_bars if b["trade_date"] > sig_date]
        future_bars = future_bars[:EXPIRY_WINDOW]

        if not future_bars and (today - sig_date).days < EXPIRY_WINDOW + 5:
            return None  # too early, no data yet

        outcome, days_to_breakout, max_gain_pct, mae_pct = _evaluate_coil_outcome(
            rec.entry_close, rec.resistance, future_bars
        )

        rec.outcome = outcome
        rec.days_to_breakout = days_to_breakout
        rec.max_gain_pct = max_gain_pct
        rec.max_adverse_excursion_pct = mae_pct
        rec.checked_thru = today.isoformat()
        if outcome == "BREAKOUT" and future_bars and days_to_breakout:
            # find the breakout bar date
            if days_to_breakout <= len(future_bars):
                rec.breakout_date = str(future_bars[days_to_breakout - 1]["trade_date"])
        return rec

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=_console, transient=True,
    ) as progress:
        task = progress.add_task(f"更新 {len(pending)} 個待確認信號...", total=len(pending))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process, rec): rec for rec in pending}
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result is not None:
                        _upsert_signal(result, db_path)
                        with update_lock:
                            updated += 1
                except Exception as e:
                    rec = futures[future]
                    logger.debug("refresh_outcomes %s: %s", rec.sig_id, e)

    return updated


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _grade_stats(
    records: list[CoilSignalRecord],
) -> dict[str, dict]:
    """Return per-grade stats dict."""
    stats: dict[str, dict] = {g: {"n": 0, "wins": 0, "pending": 0, "days": [], "maes": []} for g in GRADE_ORDER}
    for rec in records:
        if rec.grade not in stats:
            stats[rec.grade] = {"n": 0, "wins": 0, "pending": 0, "days": [], "maes": []}
        s = stats[rec.grade]
        if rec.outcome == "PENDING":
            s["pending"] += 1
            continue
        s["n"] += 1
        if rec.outcome == "BREAKOUT":
            s["wins"] += 1
            if rec.days_to_breakout is not None:
                s["days"].append(rec.days_to_breakout)
        if rec.max_adverse_excursion_pct is not None:
            s["maes"].append(rec.max_adverse_excursion_pct)
    return stats


def render_dashboard(
    records: list[CoilSignalRecord],
    grade_filter: Optional[str] = None,
    date_filter: Optional[str] = None,
    top_n: int = 5,
) -> None:
    filtered = records
    if grade_filter:
        filtered = [r for r in filtered if r.grade == grade_filter]
    if date_filter:
        filtered = [r for r in filtered if r.signal_date == date_filter]

    total = len(filtered)
    pending_count = sum(1 for r in filtered if r.outcome == "PENDING")
    settled = [r for r in filtered if r.outcome != "PENDING"]
    wins_all = sum(1 for r in settled if r.outcome == "BREAKOUT")

    # Header
    if settled:
        overall_pct = wins_all / len(settled) * 100
        overall_str = f"[bold cyan]{overall_pct:.0f}%[/bold cyan] [dim]({wins_all}/{len(settled)})[/dim]"
    else:
        overall_str = "[dim]—[/dim]"

    _console.print(Panel(
        f"[bold white]蓄積雷達追蹤看板[/bold white]\n"
        f"[dim]總信號：{total}  |  已結算：{len(settled)}  |  待確認：{pending_count}[/dim]\n"
        f"整體突破率：{overall_str}",
        title=f"[bold magenta]Coil Monitor — {date.today().isoformat()}[/bold magenta]",
        border_style="magenta",
        padding=(0, 2),
    ))

    # Grade table
    stats = _grade_stats(filtered)
    tbl = Table(
        box=box.ROUNDED, show_header=True,
        header_style="bold magenta", border_style="dim",
        title="[bold]等級突破率[/bold]", title_style="bold white",
    )
    tbl.add_column("等級", width=14)
    tbl.add_column("樣本", justify="right", width=7)
    tbl.add_column("突破率", justify="right", width=16)
    tbl.add_column("平均天數", justify="right", width=9)
    tbl.add_column("平均 MAE", justify="right", width=9)
    tbl.add_column("待確認", justify="right", width=7)

    for grade in GRADE_ORDER:
        s = stats.get(grade, {"n": 0, "wins": 0, "pending": 0, "days": [], "maes": []})
        color = GRADE_COLOR.get(grade, "white")
        wr_str = _format_winrate(s["n"], s["wins"])
        avg_days = f"{sum(s['days'])/len(s['days']):.1f}" if s["days"] else "[dim]—[/dim]"
        avg_mae = f"[red]{sum(s['maes'])/len(s['maes']):.1f}%[/red]" if s["maes"] else "[dim]—[/dim]"
        tbl.add_row(
            f"[{color}]{grade}[/{color}]",
            str(s["n"]),
            wr_str,
            avg_days,
            avg_mae,
            str(s["pending"]),
        )

    _console.print(tbl)

    # Recent breakouts
    breakouts = sorted(
        [r for r in filtered if r.outcome == "BREAKOUT"],
        key=lambda r: r.breakout_date or r.checked_thru or r.signal_date,
        reverse=True,
    )[:top_n]
    if breakouts:
        _console.print("\n[bold green]最近突破[/bold green]")
        for r in breakouts:
            gain_str = f"+{r.max_gain_pct:.1f}%" if r.max_gain_pct is not None else "--"
            mae_str = f"{r.max_adverse_excursion_pct:.1f}%" if r.max_adverse_excursion_pct is not None else "--"
            days_str = f"Day {r.days_to_breakout}" if r.days_to_breakout else "--"
            color = GRADE_COLOR.get(r.grade, "white")
            _console.print(
                f"  [{color}]{r.ticker}[/{color}] {r.name}  "
                f"[green]{gain_str}[/green]  MAE {mae_str}  {days_str}  "
                f"[dim]{r.signal_date}[/dim]"
            )

    # Recent failures
    failures = sorted(
        [r for r in filtered if r.outcome in ("STALLED", "EXPIRED")],
        key=lambda r: r.checked_thru or r.signal_date,
        reverse=True,
    )[:top_n]
    if failures:
        _console.print("\n[bold red]最近失敗[/bold red]")
        for r in failures:
            mae_str = f"{r.max_adverse_excursion_pct:.1f}%" if r.max_adverse_excursion_pct is not None else "--"
            color = GRADE_COLOR.get(r.grade, "white")
            _console.print(
                f"  [{color}]{r.ticker}[/{color}] {r.name}  "
                f"[red]MAE {mae_str}[/red]  {r.outcome}  "
                f"[dim]{r.signal_date}[/dim]"
            )


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_EXPORT_FIELDS = [
    "sig_id", "ticker", "name", "market", "signal_date", "grade", "score",
    "bb_pct", "vol_ratio", "weeks_consolidating", "vs_60d_high_pct",
    "entry_close", "resistance", "outcome", "days_to_breakout",
    "max_gain_pct", "max_adverse_excursion_pct", "checked_thru", "breakout_date",
]


def export_csv(records: list[CoilSignalRecord], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for rec in sorted(records, key=lambda r: (r.signal_date, r.ticker)):
            writer.writerow({
                "sig_id": rec.sig_id,
                "ticker": rec.ticker,
                "name": rec.name,
                "market": rec.market,
                "signal_date": rec.signal_date,
                "grade": rec.grade,
                "score": rec.score,
                "bb_pct": rec.bb_pct,
                "vol_ratio": rec.vol_ratio,
                "weeks_consolidating": rec.weeks_consolidating,
                "vs_60d_high_pct": rec.vs_60d_high_pct,
                "entry_close": rec.entry_close,
                "resistance": rec.resistance,
                "outcome": rec.outcome,
                "days_to_breakout": rec.days_to_breakout,
                "max_gain_pct": rec.max_gain_pct,
                "max_adverse_excursion_pct": rec.max_adverse_excursion_pct,
                "checked_thru": rec.checked_thru,
                "breakout_date": rec.breakout_date,
            })
    _console.print(f"\n  [green]CSV 已匯出：[/green]{output_path}  ({len(records)} 筆)")


# ---------------------------------------------------------------------------
# Auto-apply safety gate (used by optimize_coil.py)
# ---------------------------------------------------------------------------

def check_auto_apply_gate(
    db_path: Path = _DB_PATH,
    min_days: int = 90,
    min_n_per_grade: int = 50,
) -> tuple[bool, str]:
    """Return (ok, reason). Blocks --auto-apply when sample is too small."""
    if not db_path.exists():
        return False, "資料庫不存在"

    with _get_conn(db_path) as conn:
        first_row = conn.execute(
            "SELECT MIN(signal_date) as first_date FROM coil_signals"
        ).fetchone()
        if not first_row or not first_row["first_date"]:
            return False, "無信號資料"

        try:
            first_date = date.fromisoformat(first_row["first_date"])
        except ValueError:
            return False, "日期格式錯誤"

        calendar_days = (date.today() - first_date).days
        if calendar_days < min_days:
            return False, f"資料天數不足（{calendar_days}天 < {min_days}天）"

        for grade in GRADE_ORDER:
            count_row = conn.execute(
                "SELECT COUNT(*) as n FROM coil_signals WHERE grade=? AND outcome != 'PENDING'",
                (grade,),
            ).fetchone()
            n = count_row["n"] if count_row else 0
            if n < min_n_per_grade:
                return False, f"{grade} 樣本不足（{n}筆 < {min_n_per_grade}筆）"

    return True, "OK"


# ---------------------------------------------------------------------------
# Summary for bot.py integration
# ---------------------------------------------------------------------------

def get_bot_summary(db_path: Path = _DB_PATH, days: int = 30) -> dict:
    """Return a dict of stats for the Telegram bot widget."""
    if not db_path.exists():
        return {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coil_signals WHERE signal_date >= ?", (cutoff,)
        ).fetchall()
    records = [_row_to_record(r) for r in rows]
    stats = _grade_stats(records)
    pending_total = sum(s["pending"] for s in stats.values())
    new_today = sum(
        1 for r in records if r.signal_date == date.today().isoformat()
    )
    all_maes = [r.max_adverse_excursion_pct for r in records if r.max_adverse_excursion_pct is not None]
    return {
        "total": len(records),
        "pending": pending_total,
        "new_today": new_today,
        "avg_mae": sum(all_maes) / len(all_maes) if all_maes else None,
        "grade_stats": {
            g: {"n": stats[g]["n"], "wins": stats[g]["wins"]}
            for g in GRADE_ORDER
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="蓄積雷達追蹤 — 每日更新蓄積信號突破結果，顯示滾動勝率看板"
    )
    parser.add_argument("--date", default=None, help="查詢特定日期 YYYY-MM-DD")
    parser.add_argument("--grade", default=None,
                        choices=["COIL_PRIME", "COIL_MATURE", "COIL_EARLY"],
                        help="只顯示特定等級")
    parser.add_argument("--top", type=int, default=5, help="顯示最近幾筆突破/失敗（預設 5）")
    parser.add_argument("--export", default=None, metavar="FILE", help="匯出 CSV 到指定路徑")
    parser.add_argument("--refresh", action="store_true", help="強制重新查詢所有 PENDING 信號")
    parser.add_argument("--ingest", default=None, metavar="CSV",
                        help="手動匯入指定 coil_*.csv 檔")
    parser.add_argument("--db", default=None, metavar="PATH", help="指定 SQLite 路徑（預設 db/coil_track.db）")
    parser.add_argument("--days", type=int, default=30, help="顯示最近 N 天（預設 30）")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else _DB_PATH
    _ensure_schema(db_path)

    if args.ingest:
        csv_path = Path(args.ingest)
        n = ingest_coil_csv(csv_path, db_path)
        _console.print(f"  [green]已匯入 {n} 個新信號[/green] from {csv_path}")

    if args.refresh or not args.ingest:
        updated = refresh_outcomes(db_path=db_path, workers=args.workers)
        if updated:
            _console.print(f"  [dim]已更新 {updated} 個信號結果[/dim]")

    cutoff = (date.today() - timedelta(days=args.days)).isoformat()
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coil_signals WHERE signal_date >= ?", (cutoff,)
        ).fetchall()
    records = [_row_to_record(r) for r in rows]

    if not records:
        _console.print(f"[yellow]近 {args.days} 天無蓄積信號資料。[/yellow]")
        _console.print(f"  [dim]DB: {db_path}[/dim]")
        return

    render_dashboard(
        records=records,
        grade_filter=args.grade,
        date_filter=args.date,
        top_n=args.top,
    )

    if args.export:
        export_csv(records, Path(args.export))


if __name__ == "__main__":
    main()
