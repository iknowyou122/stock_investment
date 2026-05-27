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
            {"stock_id": "2330", "period_end": "2026-05-30"},
            {"stock_id": "3481", "period_end": "2026-05-20"},  # already ended
            {"stock_id": "6547", "period_end": "2026-05-22"},  # ends today — include
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
        mock_resp.json.return_value = {"data": [{"stock_id": "2330", "period_end": "2026-05-30"}]}
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
        mock_resp.json.return_value = {"data": [{"TotalExchangeMarginMaintenance": 145.2}]}
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


_INST_ROWS = [
    {"date": "2026-05-22", "stock_id": "2330", "name": "Foreign_Investor", "buy": 10_000_000, "sell": 3_000_000},
    {"date": "2026-05-22", "stock_id": "2330", "name": "Investment_Trust", "buy": 500_000, "sell": 200_000},
    {"date": "2026-05-22", "stock_id": "2330", "name": "Dealer_self", "buy": 100_000, "sell": 50_000},
    {"date": "2026-05-22", "stock_id": "2330", "name": "Dealer_Hedging", "buy": 30_000, "sell": 10_000},
    {"date": "2026-05-22", "stock_id": "2330", "name": "Foreign_Dealer_Self", "buy": 5_000, "sell": 2_000},
    # OTC ticker
    {"date": "2026-05-22", "stock_id": "6269", "name": "Foreign_Investor", "buy": 500_000, "sell": 700_000},
    {"date": "2026-05-22", "stock_id": "6269", "name": "Investment_Trust", "buy": 0, "sell": 0},
]


class TestFetchInstitutionDay:
    def _make_fetcher(self, monkeypatch) -> PaidDataFetcher:
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        return PaidDataFetcher()

    def test_parses_all_institution_types(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": _INST_ROWS}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            result = pf.fetch_institution_day(TEST_DATE)
        assert "2330" in result
        foreign, trust, dealer = result["2330"]
        assert foreign == 7_000_000   # 10M - 3M
        assert trust == 300_000        # 500K - 200K
        assert dealer == 73_000        # (100K-50K) + (30K-10K) + (5K-2K)

    def test_otc_ticker_included(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": _INST_ROWS}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            result = pf.fetch_institution_day(TEST_DATE)
        assert "6269" in result
        foreign, trust, dealer = result["6269"]
        assert foreign == -200_000  # 500K - 700K (net sell)
        assert trust == 0
        assert dealer == 0

    def test_caches_result(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": _INST_ROWS}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp) as mock_get:
            pf.fetch_institution_day(TEST_DATE)
            pf.fetch_institution_day(TEST_DATE)
        assert mock_get.call_count == 1

    def test_no_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        pf = PaidDataFetcher()
        result = pf.fetch_institution_day(TEST_DATE)
        assert result == {}

    def test_api_failure_returns_empty(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        with patch("requests.get", side_effect=Exception("network error")):
            result = pf.fetch_institution_day(TEST_DATE)
        assert result == {}
