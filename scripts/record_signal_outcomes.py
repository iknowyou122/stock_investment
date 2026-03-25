"""Nightly job: record actual D+2 / D+5 outcomes for signals in signal_outcomes table.

For each signal where signal_date = (target_date - 2 calendar days) and
actual_open_d2 IS NULL, this script fetches real OHLCV data and fills in:
  - actual_open_d2, actual_close_d2
  - actual_close_d5 (if signal_date + 5 days <= today)
  - outcome_pnl_pct, outcome_hit_target, outcome_hit_stop

Trading day approximation: D+2 = signal_date + 2 calendar days. This is a
simplification — actual trading day lookups (skipping holidays/weekends) are
deferred to Phase 4 when a TWSE calendar is integrated.

Usage:
    python scripts/record_signal_outcomes.py
    python scripts/record_signal_outcomes.py --date 2026-03-20
    python scripts/record_signal_outcomes.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Bootstrap: load .env before project imports
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient, FinMindError
from taiwan_stock_agent.infrastructure.db import init_pool, close_pool, get_connection


class _PendingSignal(NamedTuple):
    ticker: str
    signal_date: date
    entry_bid_limit: float
    stop_loss: float
    target: float


def _fetch_pending_signals(target_date: date) -> list[_PendingSignal]:
    """Return signals where signal_date = target_date - 2 days and actual_open_d2 IS NULL."""
    signal_date = target_date - timedelta(days=2)
    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, signal_date, entry_bid_limit, stop_loss, target
                FROM signal_outcomes
                WHERE signal_date = %s
                  AND actual_open_d2 IS NULL
                ORDER BY ticker
                """,
                (signal_date,),
            )
            for row in cur.fetchall():
                rows.append(
                    _PendingSignal(
                        ticker=row[0],
                        signal_date=row[1],
                        entry_bid_limit=float(row[2]),
                        stop_loss=float(row[3]),
                        target=float(row[4]),
                    )
                )
    return rows


def _count_already_filled(target_date: date) -> int:
    """Count signals for this target_date window that already have outcomes recorded."""
    signal_date = target_date - timedelta(days=2)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM signal_outcomes
                WHERE signal_date = %s
                  AND actual_open_d2 IS NOT NULL
                """,
                (signal_date,),
            )
            return cur.fetchone()[0]


def _fetch_price_on_date(
    finmind: FinMindClient,
    ticker: str,
    target_date: date,
) -> tuple[float, float] | None:
    """Return (open, close) for ticker on target_date, or None if not available."""
    # Fetch a small window so the cache doesn't need to cover the exact single day
    start = target_date - timedelta(days=5)
    end = target_date + timedelta(days=1)
    try:
        df = finmind.fetch_ohlcv(ticker, start, end)
    except FinMindError as exc:
        logger.warning("FinMind error fetching %s around %s: %s", ticker, target_date, exc)
        return None

    if df.empty:
        return None

    row = df[df["trade_date"] == target_date]
    if row.empty:
        return None

    r = row.iloc[0]
    return float(r["open"]), float(r["close"])


def _update_outcome(
    signal: _PendingSignal,
    actual_open_d2: float,
    actual_close_d2: float,
    actual_close_d5: float | None,
    d2_date: date,
    dry_run: bool,
) -> None:
    """Compute derived fields and write outcome back to signal_outcomes."""
    outcome_pnl_pct = (actual_close_d2 - signal.entry_bid_limit) / signal.entry_bid_limit * 100
    outcome_hit_target = actual_close_d2 >= signal.target
    # Gap-down stop: triggered if open_d2 <= stop_loss even before close
    outcome_hit_stop = actual_close_d2 <= signal.stop_loss or actual_open_d2 <= signal.stop_loss

    if dry_run:
        d5_str = f"{actual_close_d5:.2f}" if actual_close_d5 is not None else "N/A"
        print(
            f"  DRY RUN {signal.ticker} signal={signal.signal_date} d2={d2_date} "
            f"open_d2={actual_open_d2:.2f} close_d2={actual_close_d2:.2f} "
            f"close_d5={d5_str} pnl={outcome_pnl_pct:+.2f}% "
            f"hit_target={outcome_hit_target} hit_stop={outcome_hit_stop}"
        )
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signal_outcomes
                SET
                    execution_date     = %s,
                    actual_open_d2     = %s,
                    actual_close_d2    = %s,
                    actual_close_d5    = %s,
                    outcome_pnl_pct    = %s,
                    outcome_hit_target = %s,
                    outcome_hit_stop   = %s
                WHERE ticker = %s AND signal_date = %s
                """,
                (
                    d2_date,
                    actual_open_d2,
                    actual_close_d2,
                    actual_close_d5,
                    outcome_pnl_pct,
                    outcome_hit_target,
                    outcome_hit_stop,
                    signal.ticker,
                    signal.signal_date,
                ),
            )


def run(target_date: date, dry_run: bool) -> int:
    """Main outcome-recording run. Returns exit code."""
    print(
        f"\nSignal Outcome Recorder"
        f"\n  Target date: {target_date}"
        f"\n  Signal date: {target_date - timedelta(days=2)} (D+2 approximation)"
        f"\n  Dry run:     {dry_run}"
        f"\n"
    )

    api_key = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_KEY", "")
    if not api_key:
        print(
            "ERROR: FinMind API key not found. "
            "Set FINMIND_TOKEN in .env or as an environment variable.",
            file=sys.stderr,
        )
        return 1

    try:
        finmind = FinMindClient(api_key=api_key)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    init_pool()

    pending = _fetch_pending_signals(target_date)
    already_filled = _count_already_filled(target_date)

    if not pending:
        print(
            f"No pending signals to update "
            f"(signal_date={target_date - timedelta(days=2)}, "
            f"already_filled={already_filled})."
        )
        close_pool()
        return 0

    print(f"Found {len(pending)} pending signal(s) to update (already filled: {already_filled})\n")

    n_updated = 0
    n_skipped = 0
    d2_date = target_date  # D+2 approximation: use target_date directly

    for signal in pending:
        # D+5 date: 5 calendar days after signal_date
        d5_date = signal.signal_date + timedelta(days=5)
        fetch_d5 = d5_date <= date.today()

        # Fetch D+2 prices
        d2_prices = _fetch_price_on_date(finmind, signal.ticker, d2_date)
        if d2_prices is None:
            logger.info(
                "Skipping %s (signal=%s): D+2 price data not yet available for %s",
                signal.ticker,
                signal.signal_date,
                d2_date,
            )
            n_skipped += 1
            continue

        actual_open_d2, actual_close_d2 = d2_prices

        # Fetch D+5 close if due
        actual_close_d5: float | None = None
        if fetch_d5:
            d5_prices = _fetch_price_on_date(finmind, signal.ticker, d5_date)
            if d5_prices is not None:
                _, actual_close_d5 = d5_prices
            else:
                logger.debug(
                    "D+5 price not available for %s on %s — leaving NULL",
                    signal.ticker,
                    d5_date,
                )

        _update_outcome(
            signal=signal,
            actual_open_d2=actual_open_d2,
            actual_close_d2=actual_close_d2,
            actual_close_d5=actual_close_d5,
            d2_date=d2_date,
            dry_run=dry_run,
        )
        n_updated += 1

        pnl = (actual_close_d2 - signal.entry_bid_limit) / signal.entry_bid_limit * 100
        if not dry_run:
            print(
                f"  Updated {signal.ticker} signal={signal.signal_date}: "
                f"open_d2={actual_open_d2:.2f} close_d2={actual_close_d2:.2f} "
                f"pnl={pnl:+.2f}%"
                + (f" close_d5={actual_close_d5:.2f}" if actual_close_d5 else "")
            )

    prefix = "DRY RUN — would have updated" if dry_run else "Done."
    print(
        f"\n{prefix}"
        f"\n  Updated:  {n_updated}"
        f"\n  Skipped (data not yet available): {n_skipped}"
        f"\n  Already filled: {already_filled}"
    )
    close_pool()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nightly job: record actual D+2/D+5 outcomes for recent signals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Note: D+2 is approximated as signal_date + 2 calendar days (not trading days).
Real trading calendar support is deferred to Phase 4.

Examples:
  python scripts/record_signal_outcomes.py
  python scripts/record_signal_outcomes.py --date 2026-03-20
  python scripts/record_signal_outcomes.py --dry-run
        """,
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Target date to use as D+2 (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute outcomes but do not write to the database",
    )
    args = parser.parse_args()

    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(
                f"ERROR: Invalid date format '{args.date}' — expected YYYY-MM-DD",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        target_date = date.today()

    sys.exit(run(target_date, args.dry_run))


if __name__ == "__main__":
    main()
