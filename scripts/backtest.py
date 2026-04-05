"""Historical backtest: run TripleConfirmationEngine on past dates → signal_outcomes.

Usage:
    python scripts/backtest.py --date-from 2025-10-01 --date-to 2026-03-31
    python scripts/backtest.py --date-from 2026-01-15 --date-to 2026-01-15 --tickers 2330 2317
    make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31
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

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _is_trading_day(d: date) -> bool:
    """Exclude weekends. (Holidays not checked — TWSE will return empty data.)"""
    return d.weekday() < 5


def _date_range(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        if _is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _settle_outcomes(signal_ids: list[tuple[str, str, date]]) -> None:
    """Backfill T+1/T+3/T+5 prices for signals we just inserted.

    signal_ids: list of (signal_id, ticker, signal_date)
    """
    finmind = FinMindClient()

    with get_connection() as conn:
        for signal_id, ticker, signal_date in signal_ids:
            end = signal_date + timedelta(days=14)
            try:
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
            signal_idx = trading_days.index(signal_date)

            def get_offset(n: int) -> float | None:
                idx = signal_idx + n
                return closes[trading_days[idx]] if idx < len(trading_days) else None

            p1 = get_offset(1)
            p3 = get_offset(3)
            p5 = get_offset(5)
            entry = closes.get(signal_date)
            if entry is None:
                continue

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE signal_outcomes
                    SET price_1d = %s, price_3d = %s, price_5d = %s,
                        outcome_1d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END,
                        outcome_3d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END,
                        outcome_5d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END
                    WHERE signal_id = %s
                """, (
                    p1, p3, p5,
                    p1, p1, entry, entry,
                    p3, p3, entry, entry,
                    p5, p5, entry, entry,
                    signal_id,
                ))


def _load_watchlist_for_date(analysis_date: date, data_dir: Path) -> list[str]:
    """Load cached watchlist (industry_map) for a date, falling back up to 7 days."""
    for delta in range(0, 8):
        candidate = analysis_date - timedelta(days=delta)
        cache_file = data_dir / f"industry_map_{candidate}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                return list(json.load(f).keys())
    return []


class _EmptyLabelRepo:
    def get_label(self, branch_code: str):
        return None
    def get_labels_bulk(self, codes):
        return {}


def run_backtest(
    date_from: date,
    date_to: date,
    tickers: list[str] | None,
    settle: bool,
    delay: float,
) -> None:
    init_pool()

    agent = StrategistAgent(
        finmind=FinMindClient(),
        label_repo=_EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
    )

    data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
    trading_days = _date_range(date_from, date_to)

    total = 0
    recorded: list[tuple[str, str, date]] = []

    for day in trading_days:
        day_tickers = tickers if tickers else _load_watchlist_for_date(day, data_dir)
        if not day_tickers:
            logger.warning("No watchlist for %s, skipping", day)
            continue

        print(f"\n[{day}] scanning {len(day_tickers)} tickers...")
        for ticker in day_tickers:
            try:
                signal = agent.run(ticker, day)
                if signal.halt_flag:
                    continue
                sid = record_signal(signal, source="backtest")
                recorded.append((sid, ticker, day))
                total += 1
                if delay > 0:
                    time.sleep(delay)
            except Exception as e:
                logger.warning("skip %s %s: %s", ticker, day, e)

    print(f"\nBacktest complete: {total} signals recorded.")

    if settle and recorded:
        print(f"Settling {len(recorded)} signals (T+1/T+3/T+5)...")
        _settle_outcomes(recorded)
        print("Settlement done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical backtest → signal_outcomes")
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument("--date-to", required=True, type=date.fromisoformat)
    parser.add_argument("--tickers", nargs="*", help="Limit to specific tickers")
    parser.add_argument("--no-settle", action="store_true", help="Skip T+N outcome settlement")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between requests (default: 0.5)")
    args = parser.parse_args()

    run_backtest(
        date_from=args.date_from,
        date_to=args.date_to,
        tickers=args.tickers,
        settle=not args.no_settle,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
