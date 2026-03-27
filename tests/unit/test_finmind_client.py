"""Unit tests for FinMindClient.

All tests use unittest.mock to prevent real HTTP calls.

Coverage:
  - Constructor: raises ValueError with no API key; accepts api_key kwarg
  - halt_flag: fetch_broker_trades and fetch_ohlcv raise FinMindError when flag set
  - _is_data_ready_for: returns True after 20:00 CST on D+1; False before
  - Cache miss: _fetch called exactly once; cache hit: _fetch NOT called
  - verify_data_freshness: returns when date matches; raises on mismatch / empty data
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest

from taiwan_stock_agent.infrastructure.finmind_client import (
    FinMindClient,
    FinMindError,
    DataNotYetAvailableError,
    _is_data_ready_for,
)


# ---------------------------------------------------------------------------
# TestFinMindClientInit
# ---------------------------------------------------------------------------

class TestFinMindClientInit:
    def test_raises_if_no_api_key(self, monkeypatch):
        """No env var and no kwarg → ValueError."""
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            FinMindClient()

    def test_accepts_api_key_arg(self, monkeypatch):
        """Explicit api_key kwarg overrides missing env var."""
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        client = FinMindClient(api_key="test_key")
        assert client.api_key == "test_key"


# ---------------------------------------------------------------------------
# TestHaltFlag
# ---------------------------------------------------------------------------

class TestHaltFlag:
    def _make_client(self) -> FinMindClient:
        return FinMindClient(api_key="dummy")

    def test_halt_flag_raises_on_fetch_broker(self):
        """fetch_broker_trades raises FinMindError when halt_flag is True."""
        client = self._make_client()
        client.halt_flag = True
        with pytest.raises(FinMindError):
            client.fetch_broker_trades("2330", date(2025, 1, 1), date(2025, 1, 31))

    def test_halt_flag_raises_on_fetch_ohlcv(self):
        """fetch_ohlcv raises FinMindError when halt_flag is True."""
        client = self._make_client()
        client.halt_flag = True
        with pytest.raises(FinMindError):
            client.fetch_ohlcv("2330", date(2025, 1, 1), date(2025, 1, 31))


# ---------------------------------------------------------------------------
# TestDataFreshnessGuard
# ---------------------------------------------------------------------------

class TestDataFreshnessGuard:
    """Tests for _is_data_ready_for() — the module-level function."""

    def _patch_utcnow(self, dt: datetime):
        """Return a context manager that makes datetime.utcnow() return dt."""
        import taiwan_stock_agent.infrastructure.finmind_client as fc_module

        mock_dt = MagicMock(wraps=datetime)
        mock_dt.utcnow.return_value = dt
        return patch.object(fc_module, "datetime", mock_dt)

    def test_is_data_ready_returns_true_when_past_publish_time(self):
        """D+1 at 21:00 CST (13:00 UTC) → data is ready."""
        target_date = date(2025, 1, 15)
        # D+1 at 21:00 CST = D+1 at 13:00 UTC
        publish_day = target_date + timedelta(days=1)
        utc_time = datetime(publish_day.year, publish_day.month, publish_day.day, 13, 0, 0)

        with self._patch_utcnow(utc_time):
            assert _is_data_ready_for(target_date) is True

    def test_is_data_ready_returns_false_before_20h(self):
        """D+1 at 18:00 CST (10:00 UTC) → data is NOT ready yet."""
        target_date = date(2025, 1, 15)
        # D+1 at 18:00 CST = D+1 at 10:00 UTC
        publish_day = target_date + timedelta(days=1)
        utc_time = datetime(publish_day.year, publish_day.month, publish_day.day, 10, 0, 0)

        with self._patch_utcnow(utc_time):
            assert _is_data_ready_for(target_date) is False

    def test_is_data_ready_returns_false_if_too_early_calendar(self):
        """Same day as target_date (D+0) → always False, regardless of hour."""
        target_date = date(2025, 1, 15)
        # D+0 at 23:59 CST = D+0 at 15:59 UTC — still the same calendar day
        utc_time = datetime(
            target_date.year, target_date.month, target_date.day, 15, 59, 0
        )

        with self._patch_utcnow(utc_time):
            assert _is_data_ready_for(target_date) is False


# ---------------------------------------------------------------------------
# TestCacheLogic
# ---------------------------------------------------------------------------

class TestCacheLogic:
    """Cache hit skips _fetch; cache miss calls _fetch exactly once."""

    _START = date(2025, 1, 1)
    _END = date(2025, 1, 31)
    _TICKER = "2330"

    def _sample_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "trade_date": date(2025, 1, 15),
                    "ticker": self._TICKER,
                    "branch_code": "A001",
                    "branch_name": "元大",
                    "buy_volume": 10_000,
                    "sell_volume": 5_000,
                }
            ]
        )

    def test_cache_miss_calls_api(self, tmp_path, monkeypatch):
        """When no cache file exists, _fetch_from_api (i.e. _fetch) is called once."""
        import taiwan_stock_agent.infrastructure.finmind_client as fc_module

        monkeypatch.setattr(fc_module, "CACHE_DIR", tmp_path)

        sample_df = self._sample_df()

        with patch.object(FinMindClient, "_fetch", return_value=_broker_api_response()) as mock_fetch:
            client = FinMindClient(api_key="dummy")
            client.fetch_broker_trades(
                self._TICKER, self._START, self._END, use_cache=True
            )
            mock_fetch.assert_called_once()

    def test_cache_hit_skips_api(self, tmp_path, monkeypatch):
        """When a parquet cache file exists, _fetch is NOT called."""
        import taiwan_stock_agent.infrastructure.finmind_client as fc_module

        monkeypatch.setattr(fc_module, "CACHE_DIR", tmp_path)

        # Write a valid parquet file at the expected cache path
        expected_path = tmp_path / f"broker_trades_{self._TICKER}_{self._START}_{self._END}.parquet"
        self._sample_df().to_parquet(expected_path, index=False)

        with patch.object(FinMindClient, "_fetch") as mock_fetch:
            client = FinMindClient(api_key="dummy")
            result = client.fetch_broker_trades(
                self._TICKER, self._START, self._END, use_cache=True
            )
            mock_fetch.assert_not_called()
            assert not result.empty


# ---------------------------------------------------------------------------
# TestVerifyDataFreshness
# ---------------------------------------------------------------------------

class TestVerifyDataFreshness:
    _TARGET_DATE = date(2025, 1, 15)
    _TICKER = "2330"

    def _make_client_with_utcnow_past_publish(self) -> FinMindClient:
        """Return a FinMindClient whose datetime.utcnow resolves past publish time."""
        return FinMindClient(api_key="dummy")

    def test_verify_freshness_passes_when_date_matches(self, monkeypatch):
        """verify_data_freshness should not raise when fetch returns expected_date."""
        import taiwan_stock_agent.infrastructure.finmind_client as fc_module

        # Mock utcnow to be well after publish time (D+1 at 21:00 CST = 13:00 UTC)
        publish_day = self._TARGET_DATE + timedelta(days=1)
        utc_past = datetime(publish_day.year, publish_day.month, publish_day.day, 13, 0)
        mock_dt = MagicMock(wraps=datetime)
        mock_dt.utcnow.return_value = utc_past
        monkeypatch.setattr(fc_module, "datetime", mock_dt)

        fresh_df = pd.DataFrame([{"trade_date": self._TARGET_DATE}])
        client = FinMindClient(api_key="dummy")

        with patch.object(client, "fetch_broker_trades", return_value=fresh_df):
            # Should not raise
            client.verify_data_freshness(self._TICKER, self._TARGET_DATE)

    def test_verify_freshness_warns_when_date_mismatch(self, monkeypatch):
        """verify_data_freshness raises DataNotYetAvailableError when stale data."""
        import taiwan_stock_agent.infrastructure.finmind_client as fc_module

        # Mock utcnow to be past publish time
        publish_day = self._TARGET_DATE + timedelta(days=1)
        utc_past = datetime(publish_day.year, publish_day.month, publish_day.day, 13, 0)
        mock_dt = MagicMock(wraps=datetime)
        mock_dt.utcnow.return_value = utc_past
        monkeypatch.setattr(fc_module, "datetime", mock_dt)

        # Broker data only has a date BEFORE the expected date (stale)
        stale_date = self._TARGET_DATE - timedelta(days=1)
        stale_df = pd.DataFrame([{"trade_date": stale_date}])
        client = FinMindClient(api_key="dummy")

        with patch.object(client, "fetch_broker_trades", return_value=stale_df):
            with pytest.raises(DataNotYetAvailableError):
                client.verify_data_freshness(self._TICKER, self._TARGET_DATE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _broker_api_response() -> pd.DataFrame:
    """Minimal raw FinMind broker response (pre-rename columns)."""
    return pd.DataFrame(
        [
            {
                "date": "2025-01-15",
                "stock_id": "2330",
                "broker_id": "A001",
                "broker_name": "元大",
                "buy": 10_000,
                "sell": 5_000,
            }
        ]
    )
