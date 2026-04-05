"""Two jobs: daily scan → DB, and T+N settlement.

Usage:
    python scripts/daily_runner.py daily           # scan today → DB
    python scripts/daily_runner.py settle          # settle pending T+1/T+3/T+5
    python scripts/daily_runner.py settle --date 2026-04-03
    make daily
    make settle
    make settle DATE=2026-04-03
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job A: daily scan
# ---------------------------------------------------------------------------

def run_daily(analysis_date: date, llm: str | None, sectors: str | None) -> None:
    """Run scan for analysis_date and store results to DB."""
    from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    class _EmptyLabelRepo:
        def get_label(self, branch_code):
            return None
        def get_labels_bulk(self, codes):
            return {}

    init_pool()
    agent = StrategistAgent(
        finmind=FinMindClient(),
        label_repo=_EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
    )

    # Load watchlist from cache
    data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
    tickers: list[str] = []
    industry_map: dict[str, str] = {}
    for delta in range(0, 8):
        candidate = analysis_date - timedelta(days=delta)
        f = data_dir / f"industry_map_{candidate}.json"
        if f.exists():
            with open(f) as fh:
                industry_map = json.load(fh)
            tickers = list(industry_map.keys())
            break

    if not tickers:
        print("No watchlist cache found — run make scan first to build cache")
        return

    if sectors:
        sector_filter = [s.strip() for s in sectors.split()]
        tickers = [t for t, ind in industry_map.items() if any(s in ind for s in sector_filter)]

    print(f"[{analysis_date}] Running daily scan for {len(tickers)} tickers → DB")
    recorded = 0
    for ticker in tickers:
        try:
            signal = agent.run(ticker, analysis_date)
            if signal.halt_flag:
                continue
            record_signal(signal, source="live")
            recorded += 1
        except Exception as e:
            logger.warning("skip %s: %s", ticker, e)
        time.sleep(0.3)

    print(f"Recorded {recorded} signals to signal_outcomes (source=live)")


# ---------------------------------------------------------------------------
# Job B: settle outcomes
# ---------------------------------------------------------------------------

def run_settle(settle_date: date) -> None:
    """Backfill T+1/T+3/T+5 outcomes for signals with pending prices."""
    init_pool()
    finmind = FinMindClient()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, ticker, signal_date, entry_price
                FROM signal_outcomes
                WHERE price_5d IS NULL
                  AND halt_flag = FALSE
                  AND signal_date <= %s - INTERVAL '5 days'
                ORDER BY signal_date DESC
                LIMIT 200
            """, (settle_date,))
            rows = cur.fetchall()

    if not rows:
        print(f"[{settle_date}] Nothing to settle.")
        return

    print(f"[{settle_date}] Settling {len(rows)} signals...")

    for signal_id, ticker, signal_date, entry_price in rows:
        try:
            end = signal_date + timedelta(days=14)
            df = finmind.fetch_ohlcv(ticker, signal_date, end)
        except Exception as e:
            logger.warning("settle %s %s: %s", ticker, signal_date, e)
            continue

        if df.empty:
            continue

        closes: dict[date, float] = {}
        for _, row in df.iterrows():
            closes[row["trade_date"]] = float(row["close"])

        trading_days = sorted(closes.keys())
        if signal_date not in trading_days:
            continue

        idx = trading_days.index(signal_date)

        def get_close(offset: int) -> float | None:
            i = idx + offset
            if 0 <= i < len(trading_days):
                return closes[trading_days[i]]
            return None

        p1, p3, p5 = get_close(1), get_close(3), get_close(5)

        def outcome(p: float | None) -> float | None:
            return (p - entry_price) / entry_price if p is not None else None

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE signal_outcomes
                    SET price_1d=%s, price_3d=%s, price_5d=%s,
                        outcome_1d=%s, outcome_3d=%s, outcome_5d=%s
                    WHERE signal_id=%s AND price_5d IS NULL
                """, (p1, p3, p5, outcome(p1), outcome(p3), outcome(p5), signal_id))

        time.sleep(0.2)

    print("Settlement complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="job", required=True)

    daily_p = sub.add_parser("daily", help="Scan today and record to DB")
    daily_p.add_argument("--date", type=date.fromisoformat, default=date.today())
    daily_p.add_argument("--llm", default=None)
    daily_p.add_argument("--sectors", default=None)

    settle_p = sub.add_parser("settle", help="Backfill T+1/T+3/T+5 outcomes")
    settle_p.add_argument("--date", type=date.fromisoformat, default=date.today())

    args = parser.parse_args()
    if args.job == "daily":
        run_daily(args.date, args.llm, args.sectors)
    else:
        run_settle(args.date)


if __name__ == "__main__":
    main()
