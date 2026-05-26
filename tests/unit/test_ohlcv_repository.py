"""Unit tests for OHLCVRepository — exercises the no-op path (no DATABASE_URL)."""
from __future__ import annotations

import os
from datetime import date

import pandas as pd
import pytest

from taiwan_stock_agent.infrastructure.ohlcv_repository import OHLCVRepository


@pytest.fixture(autouse=True)
def no_db_env(monkeypatch):
    """Ensure DATABASE_URL is unset so all tests run the no-op path."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


class TestOHLCVRepositoryNoOp:
    def test_get_returns_empty_df(self):
        repo = OHLCVRepository()
        df = repo.get("2330", date(2025, 1, 1), date(2025, 1, 31))
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_max_date_returns_none(self):
        repo = OHLCVRepository()
        result = repo.max_date("2330")
        assert result is None

    def test_upsert_returns_zero(self):
        repo = OHLCVRepository()
        df = pd.DataFrame(
            {
                "trade_date": [date(2025, 1, 2)],
                "ticker": ["2330"],
                "open": [600.0],
                "high": [610.0],
                "low": [595.0],
                "close": [605.0],
                "volume": [10000],
            }
        )
        result = repo.upsert(df)
        assert result == 0

    def test_upsert_empty_df_returns_zero(self):
        repo = OHLCVRepository()
        result = repo.upsert(pd.DataFrame())
        assert result == 0

    def test_get_returns_correct_columns(self):
        repo = OHLCVRepository()
        df = repo.get("2330", date(2025, 1, 1), date(2025, 1, 31))
        expected_cols = {"trade_date", "ticker", "open", "high", "low", "close", "volume"}
        assert expected_cols == set(df.columns)

    def test_available_cached_after_first_check(self):
        repo = OHLCVRepository()
        assert repo._available is None
        repo.get("2330", date(2025, 1, 1), date(2025, 1, 2))
        assert repo._available is False

    def test_upsert_all_nan_close_returns_zero(self):
        repo = OHLCVRepository()
        df = pd.DataFrame(
            {
                "trade_date": [date(2025, 1, 2)],
                "ticker": ["2330"],
                "open": [600.0],
                "high": [610.0],
                "low": [595.0],
                "close": [float("nan")],
                "volume": [10000],
            }
        )
        result = repo.upsert(df)
        assert result == 0
