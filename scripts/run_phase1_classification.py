"""Batch Phase 1 broker label classification.

Pulls FinMind historical 分點 data for a set of tickers over a lookback window,
runs BrokerLabelClassifier.fit(), and upserts results to the broker_labels table.

Usage:
    python scripts/run_phase1_classification.py
    python scripts/run_phase1_classification.py --tickers 2330 2317 --lookback-days 365
    python scripts/run_phase1_classification.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load .env before any other project imports so DATABASE_URL and
# FINMIND_TOKEN are available when the module-level code in db.py / finmind_client.py
# reads os.environ.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass  # python-dotenv optional; caller must set env vars manually

import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Resolve project root so imports work when run directly (not via `python -m`)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient, FinMindError
from taiwan_stock_agent.infrastructure.db import init_pool, close_pool, get_connection
from taiwan_stock_agent.domain.broker_label_classifier import (
    BrokerLabelClassifier,
    PostgresBrokerLabelRepository,
)

DEFAULT_TICKERS = ["2330", "2317", "2454", "2382", "3008"]
DEFAULT_LOOKBACK_DAYS = 730
_DATA_READY_HOUR_CST = 20
_CST_OFFSET_HOURS = 8


def _apply_migrations(conn_factory) -> None:
    """Apply all SQL migrations under db/migrations/ in filename order."""
    migrations_dir = PROJECT_ROOT / "db" / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        logger.warning("No migration files found in %s", migrations_dir)
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            for sql_path in sql_files:
                logger.info("Applying migration: %s", sql_path.name)
                cur.execute(sql_path.read_text(encoding="utf-8"))
    logger.info("Migrations applied: %d file(s)", len(sql_files))


def _check_data_freshness_warning() -> None:
    """Warn if run before 20:00 CST — T+1 分點 data may not be published yet."""
    now_cst = datetime.utcnow() + timedelta(hours=_CST_OFFSET_HOURS)
    if now_cst.hour < _DATA_READY_HOUR_CST:
        print(
            f"WARNING: Current Taiwan time is {now_cst.strftime('%H:%M')} CST. "
            f"FinMind 分點 data for today may not be published yet (typically ready after "
            f"{_DATA_READY_HOUR_CST}:00 CST). "
            "Data for recent dates may be incomplete. Proceeding anyway."
        )


def _print_summary_table(labels: dict) -> None:
    """Print a sorted summary table of classification results."""
    if not labels:
        print("\nNo broker labels classified.")
        return

    sorted_labels = sorted(
        labels.values(), key=lambda b: b.reversal_rate, reverse=True
    )

    daytrade_count = sum(1 for b in labels.values() if b.label == "隔日沖")
    total = len(labels)

    print("\n" + "=" * 85)
    print(
        f"{'BRANCH CODE':<14} {'BRANCH NAME':<30} {'LABEL':<10} "
        f"{'REVERSAL RATE':>14} {'SAMPLES':>8}"
    )
    print("-" * 85)
    for bl in sorted_labels:
        print(
            f"{bl.branch_code:<14} {bl.branch_name:<30} {bl.label:<10} "
            f"{bl.reversal_rate:>13.1%} {bl.sample_count:>8,}"
        )
    print("=" * 85)
    print(f"\n隔日沖 brokers: {daytrade_count} / {total} classified")


def run(
    tickers: list[str],
    lookback_days: int,
    dry_run: bool,
) -> int:
    """Main classification run. Returns exit code (0 = success, 1 = error)."""
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    _check_data_freshness_warning()

    print(
        f"\nPhase 1 Broker Label Classification"
        f"\n  Tickers:  {', '.join(tickers)}"
        f"\n  Window:   {start_date} to {end_date} ({lookback_days} days)"
        f"\n  Dry run:  {dry_run}"
        f"\n"
    )

    # Initialize DB pool and apply migrations.
    # In dry-run mode we still attempt this (to validate schema), but skip gracefully
    # if DATABASE_URL is not configured.
    db_available = bool(os.environ.get("DATABASE_URL"))
    if db_available:
        init_pool()
        _apply_migrations(None)
    elif not dry_run:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1
    else:
        print("WARNING: DATABASE_URL not set — skipping migrations in dry-run mode.")

    # Initialize FinMind client (reads FINMIND_TOKEN or FINMIND_API_KEY from env)
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

    # Fetch data for all tickers
    import pandas as pd

    all_broker_trades: list[pd.DataFrame] = []
    all_ohlcv: list[pd.DataFrame] = []

    for ticker in tickers:
        print(f"  Fetching data for {ticker} ...", end=" ", flush=True)
        try:
            broker_df = finmind.fetch_broker_trades(ticker, start_date, end_date)
            ohlcv_df = finmind.fetch_ohlcv(ticker, start_date, end_date)
        except FinMindError as exc:
            print(f"\nERROR fetching {ticker}: {exc}", file=sys.stderr)
            if db_available:
                close_pool()
            return 1

        if broker_df.empty:
            print(f"WARN: no broker trade data returned for {ticker} — skipping.")
        else:
            all_broker_trades.append(broker_df)
            print(
                f"broker_trades={len(broker_df):,} rows, ohlcv={len(ohlcv_df):,} rows"
            )

        if not ohlcv_df.empty:
            all_ohlcv.append(ohlcv_df)

    if not all_broker_trades:
        print("ERROR: No broker trade data retrieved for any ticker.", file=sys.stderr)
        if db_available:
            close_pool()
        return 1

    combined_broker = pd.concat(all_broker_trades, ignore_index=True)
    combined_ohlcv = pd.concat(all_ohlcv, ignore_index=True) if all_ohlcv else pd.DataFrame()

    print(
        f"\nRunning BrokerLabelClassifier on "
        f"{len(combined_broker):,} broker trade rows, "
        f"{len(combined_ohlcv):,} OHLCV rows ..."
    )

    if dry_run:
        # Use an in-memory repository that captures upserts without touching DB
        class _DryRunRepo:
            def __init__(self):
                self._store: dict = {}

            def get(self, branch_code: str):
                return self._store.get(branch_code)

            def upsert(self, label) -> None:
                self._store[label.branch_code] = label

            def list_all(self):
                return list(self._store.values())

        repo = _DryRunRepo()
    else:
        repo = PostgresBrokerLabelRepository(conn_factory=None)

    classifier = BrokerLabelClassifier(repo)
    labels = classifier.fit(combined_broker, combined_ohlcv, as_of=end_date)

    if dry_run:
        print(f"\nDRY RUN — would upsert {len(labels)} broker label records (no DB writes).")
    else:
        print(f"\nUpserted {len(labels)} broker label records to broker_labels table.")

    _print_summary_table(labels)
    if db_available:
        close_pool()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1: batch broker label classification from FinMind historical data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_phase1_classification.py
  python scripts/run_phase1_classification.py --tickers 2330 2317 --lookback-days 365
  python scripts/run_phase1_classification.py --dry-run
        """,
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_TICKERS,
        metavar="TICKER",
        help=f"Ticker codes to classify (default: {' '.join(DEFAULT_TICKERS)})",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        metavar="N",
        help=f"Calendar days of history to use (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run classification without writing to the database",
    )
    args = parser.parse_args()

    sys.exit(run(args.tickers, args.lookback_days, args.dry_run))


if __name__ == "__main__":
    main()
