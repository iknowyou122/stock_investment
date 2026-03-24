"""Unit tests for BrokerLabelClassifier.

Coverage:
  - D+2 reversal rate computation (not D+1)
  - 隔日沖 label assigned when rate > 0.60 and sample_count >= 50
  - 'unknown' label when rate <= 0.60
  - 'unknown' label when sample_count < 50 (regardless of rate)
  - Branches that never appear in top-3 are not classified
  - top3 computation selects rank 1,2,3 per (ticker, trade_date)
  - reversal_rate is between 0.0 and 1.0
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from taiwan_stock_agent.domain.broker_label_classifier import (
    BrokerLabelClassifier,
    MIN_SAMPLE_COUNT,
    DAYTRADE_REVERSAL_THRESHOLD,
)


def _make_broker_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _make_ohlcv_df(ticker_close_map: dict) -> pd.DataFrame:
    """ticker_close_map: {(ticker, date): close}"""
    rows = [
        {
            "trade_date": d,
            "ticker": ticker,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": 10_000,
        }
        for (ticker, d), close in ticker_close_map.items()
    ]
    return pd.DataFrame(rows)


class _InMemoryRepo:
    def __init__(self):
        self._store = {}
    def get(self, code): return self._store.get(code)
    def upsert(self, label): self._store[label.branch_code] = label
    def list_all(self): return list(self._store.values())


def _make_historical_data(
    n_days: int,
    branch_code: str,
    branch_name: str,
    reversal_rate: float,
    ticker: str = "2330",
    base_date: date = date(2023, 1, 2),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate n_days of top-3 buyer events with given reversal_rate at D+2.

    For each day D+0:
    - Branch is top-3 buyer
    - D+2 close < D+0 close with probability = reversal_rate
    """
    # Build trading dates: n_days + 4 extra for D+2 lookups
    all_dates = [base_date + timedelta(days=i) for i in range(n_days + 4)]

    broker_rows = []
    ohlcv_map = {}

    reversals = int(n_days * reversal_rate)

    for i, d0 in enumerate(all_dates[:n_days]):
        broker_rows.append(
            {
                "trade_date": d0,
                "ticker": ticker,
                "branch_code": branch_code,
                "branch_name": branch_name,
                "buy_volume": 50_000,
                "sell_volume": 5_000,
            }
        )
        ohlcv_map[(ticker, d0)] = 100.0  # D+0 close

    # Set D+2 closes: reversal (< 100) for first `reversals` days, no reversal otherwise
    for i, d0 in enumerate(all_dates[:n_days]):
        if i + 2 < len(all_dates):
            d2 = all_dates[i + 2]
            if i < reversals:
                ohlcv_map[(ticker, d2)] = 95.0  # reversal
            else:
                ohlcv_map[(ticker, d2)] = 105.0  # no reversal

    # Fill in all remaining dates with a neutral close
    for d in all_dates:
        if (ticker, d) not in ohlcv_map:
            ohlcv_map[(ticker, d)] = 100.0

    return _make_broker_df(broker_rows), _make_ohlcv_df(ohlcv_map)


class TestClassificationLabels:
    def test_high_reversal_rate_gets_daytrade_label(self):
        broker, ohlcv = _make_historical_data(
            n_days=60,
            branch_code="DT001",
            branch_name="凱基-台北",
            reversal_rate=0.72,  # > 0.60
        )
        repo = _InMemoryRepo()
        classifier = BrokerLabelClassifier(repo)
        labels = classifier.fit(broker, ohlcv)

        assert "DT001" in labels
        assert labels["DT001"].label == "隔日沖"

    def test_low_reversal_rate_gets_unknown_label(self):
        broker, ohlcv = _make_historical_data(
            n_days=60,
            branch_code="NG001",
            branch_name="元大-板橋",
            reversal_rate=0.40,  # < 0.60
        )
        repo = _InMemoryRepo()
        classifier = BrokerLabelClassifier(repo)
        labels = classifier.fit(broker, ohlcv)

        assert "NG001" in labels
        assert labels["NG001"].label == "unknown"

    def test_borderline_reversal_rate_exactly_60pct_is_unknown(self):
        """rate must be STRICTLY > 0.60 for 隔日沖 classification."""
        broker, ohlcv = _make_historical_data(
            n_days=60,
            branch_code="BL001",
            branch_name="Border",
            reversal_rate=0.60,  # exactly at threshold — should be unknown
        )
        repo = _InMemoryRepo()
        classifier = BrokerLabelClassifier(repo)
        labels = classifier.fit(broker, ohlcv)
        # 60% exactly → not strictly > 0.60
        assert labels["BL001"].label == "unknown"

    def test_insufficient_sample_count_gets_unknown(self):
        """Branch with high rate but < MIN_SAMPLE_COUNT stays unknown."""
        broker, ohlcv = _make_historical_data(
            n_days=MIN_SAMPLE_COUNT - 1,  # one below threshold
            branch_code="SMALL001",
            branch_name="SmallBranch",
            reversal_rate=0.80,  # high rate, but not enough samples
        )
        repo = _InMemoryRepo()
        classifier = BrokerLabelClassifier(repo)
        labels = classifier.fit(broker, ohlcv)

        assert "SMALL001" in labels
        assert labels["SMALL001"].label == "unknown"
        assert labels["SMALL001"].sample_count < MIN_SAMPLE_COUNT

    def test_reversal_rate_stored_in_repo(self):
        broker, ohlcv = _make_historical_data(
            n_days=60,
            branch_code="R001",
            branch_name="RateTest",
            reversal_rate=0.70,
        )
        repo = _InMemoryRepo()
        BrokerLabelClassifier(repo).fit(broker, ohlcv)
        stored = repo.get("R001")
        assert stored is not None
        assert 0.0 <= stored.reversal_rate <= 1.0


class TestD2Horizon:
    def test_uses_d2_not_d1_for_reversal(self):
        """Verify that reversal is measured at D+2, not D+1.

        Setup: D+0 close=100, D+1 close=95 (reversal if using D+1),
               D+2 close=105 (no reversal).
        Expected: reversal_rate = 0 (using D+2).
        """
        ticker = "2330"
        base = date(2023, 1, 2)
        dates = [base + timedelta(days=i) for i in range(65)]

        broker_rows = [
            {
                "trade_date": d,
                "ticker": ticker,
                "branch_code": "TEST001",
                "branch_name": "TestBranch",
                "buy_volume": 50_000,
                "sell_volume": 0,
            }
            for d in dates[:60]
        ]

        # D+0: 100, D+1: 95 (would be reversal at D+1), D+2: 105 (no reversal at D+2)
        ohlcv_map = {}
        for i, d0 in enumerate(dates[:60]):
            ohlcv_map[(ticker, d0)] = 100.0
            if i + 1 < len(dates):
                ohlcv_map[(ticker, dates[i + 1])] = 95.0  # D+1: down
            if i + 2 < len(dates):
                ohlcv_map[(ticker, dates[i + 2])] = 105.0  # D+2: up

        ohlcv = _make_ohlcv_df(ohlcv_map)
        broker = _make_broker_df(broker_rows)

        repo = _InMemoryRepo()
        labels = BrokerLabelClassifier(repo).fit(broker, ohlcv)

        # D+2 close (105) > D+0 close (100) → no reversal → rate ≈ 0
        assert "TEST001" in labels
        assert labels["TEST001"].reversal_rate < 0.1


class TestTop3Selection:
    def test_only_top3_by_buy_volume_included_in_rate_computation(self):
        """A branch ranked 4th on most days should have lower sample_count."""
        dates = [date(2023, 1, 2) + timedelta(days=i) for i in range(60)]
        d2_dates = [date(2023, 1, 2) + timedelta(days=i) for i in range(62)]

        broker_rows = []
        ohlcv_map = {}
        ticker = "2330"

        for i, d0 in enumerate(dates):
            # TOP3 branches
            for rank, code in enumerate(["A001", "B002", "C003"]):
                broker_rows.append({
                    "trade_date": d0, "ticker": ticker,
                    "branch_code": code, "branch_name": f"Branch{code}",
                    "buy_volume": (3 - rank) * 10_000, "sell_volume": 0,
                })
            # RANK 4 branch — should NOT appear in top3
            broker_rows.append({
                "trade_date": d0, "ticker": ticker,
                "branch_code": "D004", "branch_name": "BranchD",
                "buy_volume": 5_000, "sell_volume": 0,
            })
            ohlcv_map[(ticker, d0)] = 100.0
            if i + 2 < len(d2_dates):
                ohlcv_map[(ticker, d2_dates[i + 2])] = 95.0  # reversal for D+2

        for d in d2_dates:
            if (ticker, d) not in ohlcv_map:
                ohlcv_map[(ticker, d)] = 100.0

        ohlcv = _make_ohlcv_df(ohlcv_map)
        broker = _make_broker_df(broker_rows)

        repo = _InMemoryRepo()
        labels = BrokerLabelClassifier(repo).fit(broker, ohlcv)

        # D004 was always rank 4 → should have 0 sample_count (not in top3)
        assert "D004" not in labels or labels["D004"].sample_count == 0
