"""Unit tests for PaidDataFetcher — all HTTP calls mocked."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher


TEST_DATE = date(2026, 5, 22)


class TestPaidDataFetcherInit:
    def test_no_key_returns_empty_sets(self, monkeypatch):
        """Without API key, all fetch methods return empty frozenset silently."""
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        pf = PaidDataFetcher()
        assert pf.fetch_disposal_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_halt_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_limit_up_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_daytrade_restricted_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_market_margin_maintenance(TEST_DATE) is None

    def test_with_key_reads_from_env(self, monkeypatch):
        """With API key in env, _api_key is populated."""
        monkeypatch.setenv("FINMIND_API_KEY", "test_key_123")
        pf = PaidDataFetcher()
        assert pf._api_key == "test_key_123"


class TestFetchDisposalTickers:
    def _make_fetcher(self, monkeypatch) -> PaidDataFetcher:
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        return PaidDataFetcher()

    def test_returns_tickers_still_under_disposition(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [
            {"stock_id": "2330", "end_date": "2026-05-30"},
            {"stock_id": "3481", "end_date": "2026-05-20"},  # already ended
            {"stock_id": "6547", "end_date": "2026-05-22"},  # ends today — include
        ]}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            result = pf.fetch_disposal_tickers(TEST_DATE)
        assert "2330" in result
        assert "3481" not in result  # ended before TEST_DATE
        assert "6547" in result

    def test_caches_result(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"stock_id": "2330", "end_date": "2026-05-30"}]}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp) as mock_get:
            pf.fetch_disposal_tickers(TEST_DATE)
            pf.fetch_disposal_tickers(TEST_DATE)  # second call — should use cache
        assert mock_get.call_count == 1  # only one HTTP call

    def test_api_failure_returns_empty(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        with patch("requests.get", side_effect=Exception("network error")):
            result = pf.fetch_disposal_tickers(TEST_DATE)
        assert result == frozenset()


class TestFetchMarketMarginMaintenance:
    def test_parses_rate_field(self, monkeypatch):
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        pf = PaidDataFetcher()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"margin_maintenance_ratio": 145.2}]}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            rate = pf.fetch_market_margin_maintenance(TEST_DATE)
        assert rate == pytest.approx(145.2)

    def test_returns_none_on_empty_data(self, monkeypatch):
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        pf = PaidDataFetcher()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            assert pf.fetch_market_margin_maintenance(TEST_DATE) is None
