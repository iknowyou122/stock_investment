"""Step zero: verify FinMind date alignment.

Confirms that TaiwanStockBrokerTradingStatement settlement dates align with
TaiwanStockPriceAdj trade dates for a known ticker/date pair.

Misaligned join keys produce systematic false signals — this check must pass
before running any analysis or the spike validation.

Usage:
    python scripts/data_alignment_check.py
    python scripts/data_alignment_check.py --ticker 2330 --date 2025-01-15
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running from project root without installing package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient


def run_ohlcv_check(ticker: str, check_date: date) -> bool:
    """Verify OHLCV data is fetchable and the check_date is present."""
    client = FinMindClient()
    start = check_date - timedelta(days=5)
    end = check_date + timedelta(days=2)

    print(f"Fetching OHLCV ({start} → {end}) ...")
    ohlcv_df = client.fetch_ohlcv(ticker, start, end, use_cache=False)

    if ohlcv_df.empty:
        print("❌ FAIL: No OHLCV data returned.")
        return False

    ohlcv_dates = set(ohlcv_df["trade_date"].unique())
    print(f"OHLCV trade dates: {sorted(ohlcv_dates)}")

    if check_date not in ohlcv_dates:
        print(f"\n❌ FAIL: {check_date} not found in OHLCV dates.")
        return False

    ohlcv_row = ohlcv_df[ohlcv_df["trade_date"] == check_date]
    print(f"\nOHLCV for {check_date}:")
    print(ohlcv_row[["trade_date", "ticker", "open", "high", "low", "close", "volume"]].to_string(index=False))
    print(f"\n✅ PASS: OHLCV data aligned for {ticker} on {check_date}")
    return True


def run_alignment_check(ticker: str, check_date: date, ohlcv_only: bool = False) -> bool:
    """Return True if alignment check passes, False otherwise."""
    print(f"\n=== FinMind Date Alignment Check ===")
    print(f"Ticker: {ticker} | Check date: {check_date}")
    if ohlcv_only:
        print("Mode: OHLCV-only (broker data skipped)")
    print()

    if ohlcv_only:
        return run_ohlcv_check(ticker, check_date)

    client = FinMindClient()
    start = check_date - timedelta(days=5)
    end = check_date + timedelta(days=2)

    print(f"Fetching broker trades ({start} → {end}) ...")
    broker_df = client.fetch_broker_trades(ticker, start, end, use_cache=False)

    print(f"Fetching OHLCV ({start} → {end}) ...")
    ohlcv_df = client.fetch_ohlcv(ticker, start, end, use_cache=False)

    if broker_df.empty:
        print("❌ FAIL: No broker trade data returned.")
        return False
    if ohlcv_df.empty:
        print("❌ FAIL: No OHLCV data returned.")
        return False

    broker_dates = set(broker_df["trade_date"].unique())
    ohlcv_dates = set(ohlcv_df["trade_date"].unique())

    print(f"Broker trade dates: {sorted(broker_dates)}")
    print(f"OHLCV trade dates:  {sorted(ohlcv_dates)}")

    common = broker_dates & ohlcv_dates
    broker_only = broker_dates - ohlcv_dates
    ohlcv_only_dates = ohlcv_dates - broker_dates

    print(f"\nOverlapping dates: {sorted(common)}")
    if broker_only:
        print(f"⚠ Broker-only dates (no OHLCV match): {sorted(broker_only)}")
    if ohlcv_only_dates:
        print(f"⚠ OHLCV-only dates (no broker match): {sorted(ohlcv_only_dates)}")

    if check_date not in broker_dates:
        print(f"\n❌ FAIL: {check_date} not found in broker trade dates.")
        print("  → Check if market was open on this date.")
        return False
    if check_date not in ohlcv_dates:
        print(f"\n❌ FAIL: {check_date} not found in OHLCV dates.")
        return False

    broker_row = broker_df[broker_df["trade_date"] == check_date].head(3)
    ohlcv_row = ohlcv_df[ohlcv_df["trade_date"] == check_date]

    print(f"\nSample broker rows for {check_date}:")
    print(broker_row[["trade_date", "branch_code", "branch_name", "buy_volume", "sell_volume"]].to_string(index=False))

    print(f"\nOHLCV for {check_date}:")
    print(ohlcv_row[["trade_date", "ticker", "open", "high", "low", "close", "volume"]].to_string(index=False))

    print(f"\n✅ PASS: date join keys aligned for {ticker} on {check_date}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify FinMind date alignment")
    parser.add_argument("--ticker", default="2330", help="Ticker to check (default: 2330)")
    parser.add_argument(
        "--date",
        default="2025-01-15",
        help="Date to verify (default: 2025-01-15)",
    )
    parser.add_argument(
        "--ohlcv-only",
        action="store_true",
        help="Skip broker data check (use when TaiwanStockBrokerTradingStatement is unavailable)",
    )
    args = parser.parse_args()

    check_date = date.fromisoformat(args.date)
    passed = run_alignment_check(args.ticker, check_date, ohlcv_only=args.ohlcv_only)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
