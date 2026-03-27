"""settle_outcomes.py — Cron script to fill D+1/D+3/D+5 price outcomes.

Run daily (e.g. via cron or GitHub Actions):
    python scripts/settle_outcomes.py

Reads DATABASE_URL and FINMIND_API_KEY from .env (via python-dotenv).

Logic:
  1. Fetch all unsettled signal rows (price_5d IS NULL, halt_flag IS FALSE,
     created within last 10 calendar days).
  2. For each row, determine which price columns need filling based on
     calendar days elapsed since created_at:
       price_1d if >= 1 day elapsed
       price_3d if >= 3 days elapsed
       price_5d if >= 5 days elapsed
  3. Fetch OHLCV from FinMind for the target date and write the close price.
  4. Print a summary.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv()

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.signal_outcome_repo import SignalOutcomeRepository


def _target_date(signal_date: date, day_offset: int) -> date:
    """Return the target date as signal_date + day_offset calendar days."""
    return signal_date + timedelta(days=day_offset)


def _fetch_close(finmind: FinMindClient, ticker: str, target: date) -> float | None:
    """Fetch the closing price for ticker on target date. Returns None on failure."""
    try:
        df = finmind.fetch_ohlcv(ticker, target, target)
    except Exception as exc:
        print(f"  [WARN] FinMind fetch failed for {ticker} on {target}: {exc}")
        return None

    if df is None or df.empty:
        return None

    rows = df[df["trade_date"] == target]
    if rows.empty:
        return None

    return float(rows.iloc[-1]["close"])


def main() -> None:
    api_key = os.environ.get("FINMIND_API_KEY", "")
    finmind = FinMindClient(api_key=api_key)
    repo = SignalOutcomeRepository()

    unsettled = repo.fetch_unsettled(days_back=10)
    print(f"settle_outcomes: {len(unsettled)} unsettled rows found.")

    now = datetime.utcnow()
    updated_count = 0

    for row in unsettled:
        signal_id: str = row["signal_id"]
        ticker: str = row["ticker"]
        signal_date: date = row["signal_date"]
        created_at: datetime = row["created_at"]

        # Ensure created_at is timezone-naive for arithmetic
        if hasattr(created_at, "tzinfo") and created_at.tzinfo is not None:
            created_at = created_at.replace(tzinfo=None)

        elapsed_days = (now - created_at).days

        columns_to_fill: list[tuple[str, date]] = []
        if elapsed_days >= 1:
            columns_to_fill.append(("price_1d", _target_date(signal_date, 1)))
        if elapsed_days >= 3:
            columns_to_fill.append(("price_3d", _target_date(signal_date, 3)))
        if elapsed_days >= 5:
            columns_to_fill.append(("price_5d", _target_date(signal_date, 5)))

        for col, target in columns_to_fill:
            close = _fetch_close(finmind, ticker, target)
            if close is None:
                print(
                    f"  [SKIP] {ticker} {signal_id[:8]}... {col} — "
                    f"no data for {target}"
                )
                continue

            try:
                repo.fill_price(signal_id, col, close)
                print(
                    f"  [OK]   {ticker} {signal_id[:8]}... {col} = {close:.2f} "
                    f"(target date: {target})"
                )
                updated_count += 1
            except Exception as exc:
                print(
                    f"  [ERR]  {ticker} {signal_id[:8]}... {col} — "
                    f"fill_price failed: {exc}"
                )

    print(f"\nDone. {updated_count} price column(s) updated.")


if __name__ == "__main__":
    main()
