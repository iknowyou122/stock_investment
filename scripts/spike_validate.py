"""Pre-Phase-1 Validation Spike: validate 隔日沖 reversal rate hypothesis.

Goal: confirm that branches widely known as 隔日沖 shops show >60% reversal rate
at D+2 execution horizon (earliest tradeable day after FinMind T+1 data is available).

Measurement:
  On days when a branch is in the top-3 buyers of a stock:
  - D+0: the trade date
  - D+1: FinMind publishes data (not tradeable)
  - D+2: earliest execution; reversal = (D+2 close < D+0 close)

  Target: reversal_rate > 0.60 for known 隔日沖 branches (vs ~0.35 unconditional baseline).

Usage:
    python scripts/spike_validate.py
    python scripts/spike_validate.py --tickers 2330 2317 --start 2023-01-01 --end 2024-12-31

Output:
  - Per-branch reversal rates at D+2
  - Unconditional baseline for comparison
  - Pass/fail verdict

Before running: ensure scripts/data_alignment_check.py passes first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.domain.broker_label_classifier import BrokerLabelClassifier

# Branches widely known in trader community as 隔日沖 shops
KNOWN_DAYTRADE_BRANCHES = [
    "凱基-台北",
    "摩根大通",
    "美林",
    "法興",
    "元大-板橋",
]

# Tickers for the spike (high-liquidity, well-covered by broker data)
DEFAULT_TICKERS = ["2330", "2317", "2454", "2382", "3008", "2308", "2303", "2412"]

REVERSAL_THRESHOLD = 0.60
MIN_SAMPLE_COUNT = 50


def compute_unconditional_baseline(ohlcv: pd.DataFrame) -> float:
    """Compute unconditional P(D+2 close < D+0 close) across all stocks and dates."""
    # Build close map: (ticker, date) → close
    close_map = ohlcv.set_index(["ticker", "trade_date"])["close"].to_dict()

    # Build trading date lists per ticker
    trading_dates: dict[str, list] = {}
    for ticker, grp in ohlcv.groupby("ticker"):
        trading_dates[ticker] = sorted(grp["trade_date"].unique())

    reversals = 0
    total = 0
    for ticker, dates in trading_dates.items():
        for i, d0 in enumerate(dates):
            if i + 2 >= len(dates):
                continue
            d2 = dates[i + 2]
            c0 = close_map.get((ticker, d0))
            c2 = close_map.get((ticker, d2))
            if c0 is None or c2 is None:
                continue
            total += 1
            if c2 < c0:
                reversals += 1

    if total == 0:
        return 0.0
    return reversals / total


def run_spike(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> None:
    print("\n=== 隔日沖 Reversal Rate Validation Spike ===")
    print(f"Tickers: {tickers}")
    print(f"Period: {start_date} → {end_date}")
    print(f"Execution horizon: D+2 (earliest tradeable after FinMind T+1 publish)")
    print()

    client = FinMindClient()
    all_broker = []
    all_ohlcv = []

    for ticker in tickers:
        print(f"  Fetching {ticker} ...")
        try:
            b = client.fetch_broker_trades(ticker, start_date, end_date)
            o = client.fetch_ohlcv(ticker, start_date, end_date)
            if not b.empty:
                all_broker.append(b)
            if not o.empty:
                all_ohlcv.append(o)
        except Exception as e:
            print(f"  WARNING: failed to fetch {ticker}: {e}")

    if not all_broker or not all_ohlcv:
        print("❌ FAIL: No data fetched. Check FINMIND_API_KEY and network.")
        sys.exit(1)

    broker_df = pd.concat(all_broker, ignore_index=True)
    ohlcv_df = pd.concat(all_ohlcv, ignore_index=True)

    print(f"\nData loaded: {len(broker_df):,} broker trade rows, {len(ohlcv_df):,} OHLCV rows")

    # --- Unconditional baseline ---
    baseline = compute_unconditional_baseline(ohlcv_df)
    print(f"\nUnconditional baseline (all stocks, D+2 reversal rate): {baseline:.1%}")
    print(f"Required: >60% for 隔日沖 signal to be meaningful (vs {baseline:.0%} baseline)")

    # --- Branch-level analysis using BrokerLabelClassifier internals ---
    # Use an in-memory repo (no DB needed for spike)
    from typing import Protocol

    class _InMemoryRepo:
        def get(self, code): return None
        def upsert(self, label): pass
        def list_all(self): return []

    classifier = BrokerLabelClassifier(_InMemoryRepo())
    top3 = classifier._compute_top3_buyers(broker_df)
    rates_df = classifier._compute_reversal_rates(top3, ohlcv_df)

    if rates_df.empty:
        print("❌ FAIL: Could not compute reversal rates. Check data alignment.")
        sys.exit(1)

    # Filter to branches with sufficient samples
    qualified = rates_df[rates_df["sample_count"] >= MIN_SAMPLE_COUNT].copy()
    qualified = qualified.sort_values("reversal_rate", ascending=False)

    print(f"\n{'='*70}")
    print(f"{'Branch':<30} {'Reversal Rate':>15} {'Samples':>10} {'Signal?':>10}")
    print(f"{'-'*70}")

    for _, row in qualified.iterrows():
        name = row["branch_name"]
        rate = row["reversal_rate"]
        n = int(row["sample_count"])
        signal = "✅ 隔日沖" if rate > REVERSAL_THRESHOLD else "—"
        known_marker = " ← KNOWN" if name in KNOWN_DAYTRADE_BRANCHES else ""
        print(f"{name:<30} {rate:>14.1%} {n:>10,} {signal:>10}{known_marker}")

    # --- Verdict ---
    print(f"\n{'='*70}")
    daytrade_classified = qualified[qualified["reversal_rate"] > REVERSAL_THRESHOLD]
    known_classified = [
        r for _, r in daytrade_classified.iterrows()
        if r["branch_name"] in KNOWN_DAYTRADE_BRANCHES
    ]

    print(f"\nResults:")
    print(f"  Qualified branches (n≥{MIN_SAMPLE_COUNT}): {len(qualified)}")
    print(f"  Classified as 隔日沖 (rate>{REVERSAL_THRESHOLD:.0%}): {len(daytrade_classified)}")
    print(
        f"  Known 隔日沖 branches correctly classified: "
        f"{len(known_classified)}/{len(KNOWN_DAYTRADE_BRANCHES)}"
    )

    if len(known_classified) >= 2 and len(daytrade_classified) >= 3:
        print("\n✅ HYPOTHESIS CONFIRMED: 隔日沖 moat validated at D+2 execution horizon.")
        print("   Proceed to Phase 1: build full broker label database.")
    elif len(daytrade_classified) >= 1:
        print("\n⚠ PARTIAL: Some 隔日沖 branches classified but signal is weak.")
        print("   Review feature set before Phase 1.")
    else:
        print("\n❌ HYPOTHESIS REJECTED: No branches show >60% reversal at D+2.")
        print("   Revisit classifier criteria before building Phase 1.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate 隔日沖 reversal rate hypothesis")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--start", default="2023-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    args = parser.parse_args()

    run_spike(
        tickers=args.tickers,
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
    )


if __name__ == "__main__":
    main()
