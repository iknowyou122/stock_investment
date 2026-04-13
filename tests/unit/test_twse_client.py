"""Unit tests for ChipProxyFetcher (TWSE free-tier chip proxy).

Coverage targets:
  - Successful fetch → TWSEChipProxy populated
  - Network failure → zero-value proxy, no exception raised
  - Partial data (foreign present, margin missing) → partial proxy + flag
  - Cache hit → no HTTP call made on second fetch
"""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_t86_response(ticker: str, net_buy: int, trust_buy: int = 0, dealer_buy: int = 0) -> dict:
    """Minimal valid TWSE T86 JSON response including 投信買賣超股數 and 自營商買賣超股數."""
    return {
        "stat": "OK",
        "fields": ["證券代號", "外陸資買賣超股數", "投信買賣超股數", "自營商買賣超股數"],
        "data": [
            [
                ticker,
                f"+{net_buy:,}" if net_buy >= 0 else f"{net_buy:,}",
                f"+{trust_buy:,}" if trust_buy >= 0 else f"{trust_buy:,}",
                f"+{dealer_buy:,}" if dealer_buy >= 0 else f"{dealer_buy:,}",
            ],
        ],
    }


def _make_mi_margn_openapi_response(
    ticker: str,
    today_margin: int = 0,
    prev_margin: int = 0,
    today_short: int = 0,
    prev_short: int = 0,
    margin_limit: int = 0,
) -> list:
    """Minimal openapi MI_MARGN list response."""
    return [
        {
            "股票代號": ticker,
            "股票名稱": "Test",
            "融資今日餘額": str(today_margin),
            "融資前日餘額": str(prev_margin),
            "融券今日餘額": str(today_short),
            "融券前日餘額": str(prev_short),
            "融資限額": str(margin_limit) if margin_limit else "",
            "融券限額": "",
        }
    ]


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestChipProxyFetcherSuccess:
    def test_valid_response_populates_proxy(self):
        """Both endpoints return valid data → TWSEChipProxy fully populated."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response(ticker, net_buy=5_000_000, trust_buy=200_000, dealer_buy=100_000)
        t86_resp.raise_for_status = MagicMock()

        margn_resp = MagicMock()
        margn_resp.json.return_value = _make_mi_margn_openapi_response(
            ticker, today_margin=10_000, prev_margin=12_000
        )
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = _side_effect
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.is_available is True
        assert proxy.foreign_net_buy == 5_000_000
        assert proxy.trust_net_buy == 200_000
        assert proxy.dealer_net_buy == 100_000
        assert proxy.margin_balance_change == -2_000  # 10_000 - 12_000
        assert proxy.ticker == ticker
        assert proxy.trade_date == trade_date


class TestChipProxyFetcherNetworkFailure:
    def test_connection_error_returns_unavailable_proxy(self):
        """ConnectionError → is_available=False, no exception raised."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = ConnectionError("network down")
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.is_available is False
        assert proxy.foreign_net_buy == 0
        assert proxy.margin_balance_change == 0
        assert any("T86_ERROR" in f or "MARGN_ERROR" in f for f in proxy.data_quality_flags)

    def test_timeout_returns_unavailable_proxy(self):
        """Timeout → is_available=False, no exception raised."""
        ticker = "9999"
        trade_date = date(2026, 3, 24)

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = TimeoutError("timed out")
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.is_available is False


class TestChipProxyFetcherPartialData:
    def test_foreign_present_margin_missing_flags_partial(self):
        """T86 returns data, MI_MARGN returns empty list → partial proxy with flag."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response(ticker, net_buy=3_000_000, trust_buy=100_000)
        t86_resp.raise_for_status = MagicMock()

        # openapi MI_MARGN returns empty list (ticker not found)
        margn_resp = MagicMock()
        margn_resp.json.return_value = []
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = _side_effect
                proxy = fetcher.fetch(ticker, trade_date)

        # Foreign is available, so is_available=True (at least one source)
        assert proxy.is_available is True
        assert proxy.foreign_net_buy == 3_000_000
        assert proxy.trust_net_buy == 100_000
        assert proxy.margin_balance_change == 0  # default

    def test_trust_net_buy_parsed_from_t86(self):
        """T86 response with 投信買賣超股數 → trust_net_buy populated."""
        ticker = "2454"
        trade_date = date(2026, 3, 24)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response(ticker, net_buy=-500_000, trust_buy=300_000)
        t86_resp.raise_for_status = MagicMock()

        # openapi MI_MARGN returns empty list — only T86 matters here
        margn_resp = MagicMock()
        margn_resp.json.return_value = []
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = _side_effect
                proxy = fetcher.fetch(ticker, trade_date)

        # 外資 net sell but 投信 net buy — proxy still available
        assert proxy.is_available is True
        assert proxy.foreign_net_buy == -500_000
        assert proxy.trust_net_buy == 300_000

    def test_t86_without_trust_column_still_returns_foreign(self):
        """T86 response lacking 投信買賣超股數 column → foreign populated, trust=0."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        # Response without trust column
        t86_no_trust = {
            "stat": "OK",
            "fields": ["證券代號", "外陸資買賣超股數"],
            "data": [["2330", "+1,000,000"]],
        }

        t86_resp = MagicMock()
        t86_resp.json.return_value = t86_no_trust
        t86_resp.raise_for_status = MagicMock()

        margn_resp = MagicMock()
        margn_resp.json.return_value = []
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = _side_effect
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.foreign_net_buy == 1_000_000
        assert proxy.trust_net_buy == 0  # column absent → default
        assert proxy.dealer_net_buy == 0  # column absent → default

    def test_dealer_net_buy_parsed_from_t86(self):
        """T86 response with 自營商買賣超股數 → dealer_net_buy populated."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response(
            ticker, net_buy=1_000_000, trust_buy=0, dealer_buy=150_000
        )
        t86_resp.raise_for_status = MagicMock()

        margn_resp = MagicMock()
        margn_resp.json.return_value = []
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = _side_effect
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.is_available is True
        assert proxy.foreign_net_buy == 1_000_000
        assert proxy.dealer_net_buy == 150_000

    def test_t86_without_dealer_column_still_returns_other_fields(self):
        """T86 response lacking 自營商買賣超股數 column → foreign+trust populated, dealer=0."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        t86_no_dealer = {
            "stat": "OK",
            "fields": ["證券代號", "外陸資買賣超股數", "投信買賣超股數"],
            "data": [["2330", "+2,000,000", "+100,000"]],
        }

        t86_resp = MagicMock()
        t86_resp.json.return_value = t86_no_dealer
        t86_resp.raise_for_status = MagicMock()

        margn_resp = MagicMock()
        margn_resp.json.return_value = []
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = _side_effect
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.foreign_net_buy == 2_000_000
        assert proxy.trust_net_buy == 100_000
        assert proxy.dealer_net_buy == 0  # column absent → default


class TestChipProxyFetcherCache:
    def test_second_fetch_within_ttl_skips_http(self):
        """If all cache files exist from a prior fetch, no HTTP call is made."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)  # Monday

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            # Main T86 cache (today)
            t86_cache = Path(tmpdir) / f"twse_t86_{ticker}_{trade_date}.parquet"
            pd.DataFrame([{"foreign_net_buy": 2_000_000, "trust_net_buy": 50_000, "dealer_net_buy": 30_000}]).to_parquet(t86_cache, index=False)

            # Unified margin row cache (replaces separate today/prev/short cache files)
            pd.DataFrame([{
                "today_margin": 8_000,
                "prev_margin": 10_000,
                "today_short": 5_000,
                "prev_short": 4_000,
                "margin_limit": 0,
            }]).to_parquet(
                Path(tmpdir) / f"twse_margin_row_{ticker}_{trade_date}.parquet", index=False
            )

            # T86 lookback caches (offsets 1-14) — weekends get a "no_data" sentinel
            # that causes the cache to return (None, None, None) without HTTP call.
            for offset in range(1, 15):
                d = trade_date - timedelta(days=offset)
                path = Path(tmpdir) / f"twse_t86_{ticker}_{d}.parquet"
                if d.weekday() >= 5:  # Sat / Sun
                    pd.DataFrame([{"no_data": True}]).to_parquet(path, index=False)
                else:
                    pd.DataFrame([{"foreign_net_buy": 1_000_000, "trust_net_buy": 50_000, "dealer_net_buy": 30_000}]).to_parquet(path, index=False)

            # SBL cache (Tier A new endpoint)
            pd.DataFrame([{"sbl_ratio": 0.05}]).to_parquet(
                Path(tmpdir) / f"twse_sbl_{ticker}_{trade_date}.parquet", index=False
            )

            # Margin utilization cache (Tier A new endpoint)
            pd.DataFrame([{"margin_utilization": 0.30}]).to_parquet(
                Path(tmpdir) / f"twse_margin_util_{ticker}_{trade_date}.parquet", index=False
            )

            # Daytrade ratio cache (Tier A new endpoint)
            pd.DataFrame([{"daytrade_ratio": 0.12}]).to_parquet(
                Path(tmpdir) / f"twse_daytrade_{ticker}_{trade_date}.parquet", index=False
            )

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                proxy = fetcher.fetch(ticker, trade_date)
                mock_get.assert_not_called()

        assert proxy.is_available is True
        assert proxy.foreign_net_buy == 2_000_000
        assert proxy.trust_net_buy == 50_000
        assert proxy.dealer_net_buy == 30_000
        assert proxy.margin_balance_change == -2_000          # 8_000 - 10_000
        assert proxy.foreign_consecutive_buy_days == 7        # today + 6 prior trading days
        assert proxy.short_balance_increased is True          # 5_000 > 4_000 * 1.20 = 4_800
        assert proxy.short_margin_ratio == pytest.approx(5_000 / 8_000)  # today_short / today_margin


# ------------------------------------------------------------------
# Factor 5: 外資連買天數 (foreign_consecutive_buy_days)
# ------------------------------------------------------------------

def _make_t86_response_for_date(ticker: str, foreign: int) -> dict:
    """Helper: minimal T86 response with a single row."""
    return {
        "stat": "OK",
        "fields": ["證券代號", "外陸資買賣超股數"],
        "data": [
            [ticker, f"+{foreign:,}" if foreign >= 0 else f"{foreign:,}"],
        ],
    }


class TestForeignConsecutiveDays:
    def test_three_consecutive_buy_days(self):
        """T86 shows positive foreign net buy on trade_date and two prior days → count = 3."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)  # Wednesday

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            d = params["date"]
            if d == "20260326":
                mock_resp.json.return_value = _make_t86_response_for_date(ticker, 1_000_000)
            elif d == "20260325":
                mock_resp.json.return_value = _make_t86_response_for_date(ticker, 500_000)
            elif d == "20260324":
                mock_resp.json.return_value = _make_t86_response_for_date(ticker, 200_000)
            elif d == "20260323":
                mock_resp.json.return_value = _make_t86_response_for_date(ticker, -100_000)  # sold
            else:
                mock_resp.json.return_value = {"stat": "NO_DATA"}
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.foreign_consecutive_buy_days == 3

    def test_foreign_sell_today_gives_zero(self):
        """If today's foreign net buy is negative, consecutive count is 0."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _make_t86_response_for_date(ticker, -1_000_000)
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.foreign_consecutive_buy_days == 0

    def test_no_t86_data_returns_zero(self):
        """If T86 returns no data at all, consecutive count is 0 (not an error)."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"stat": "NO_DATA"}
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.foreign_consecutive_buy_days == 0

    def test_caps_at_seven_trading_days(self):
        """Lookback stops collecting after 7 trading days; count is capped at 7."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)  # Wednesday

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            # All T86 days show positive net buy
            mock_resp.json.return_value = _make_t86_response_for_date(ticker, 1_000_000)
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.foreign_consecutive_buy_days == 7


# ------------------------------------------------------------------
# Factor 7: 融券餘額 + 券資比 (short_balance_increased, short_margin_ratio)
# ------------------------------------------------------------------

class TestShortBalanceData:
    def test_short_spike_detected(self):
        """When today's 融券餘額 > yesterday's × 1.20, short_balance_increased is True."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
        t86_resp.raise_for_status = MagicMock()

        # Single openapi call returns both today and prev values
        margn_resp = MagicMock()
        margn_resp.json.return_value = _make_mi_margn_openapi_response(
            ticker, today_margin=10_000, prev_margin=10_000, today_short=6_000, prev_short=4_000
        )
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            def _short_side_effect(url, **kwargs):
                if "T86" in url:
                    return t86_resp
                return margn_resp

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       side_effect=_short_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        # 6_000 > 4_000 * 1.20 = 4_800 → True
        assert proxy.short_balance_increased is True
        assert proxy.short_margin_ratio == pytest.approx(6_000 / 10_000)

    def test_no_short_spike_when_increase_below_threshold(self):
        """When short balance grows < 20%, short_balance_increased is False."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        margn_resp = MagicMock()
        margn_resp.json.return_value = _make_mi_margn_openapi_response(
            ticker, today_margin=10_000, prev_margin=10_000, today_short=4_500, prev_short=4_000
        )
        margn_resp.raise_for_status = MagicMock()

        def _side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            if "T86" in url:
                mock_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
                return mock_resp
            return margn_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        # 4_500 > 4_000 * 1.20 = 4_800 → False
        assert proxy.short_balance_increased is False
        assert proxy.short_margin_ratio == pytest.approx(4_500 / 10_000)

    def test_short_data_unavailable_returns_defaults(self):
        """When openapi MI_MARGN returns empty list, short fields default to safe values."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        def _side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            if "T86" in url:
                mock_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
            else:
                # openapi returns empty list — ticker not margin-eligible
                mock_resp.json.return_value = []
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.short_balance_increased is False
        assert proxy.short_margin_ratio == 0.0


# ------------------------------------------------------------------
# Tier A: _fetch_institution_consecutive_days (trust + dealer)
# ------------------------------------------------------------------

class TestInstitutionConsecutiveDays:
    def test_fetch_institution_consecutive_days_returns_all_three(self):
        """_fetch_institution_consecutive_days returns (foreign, trust, dealer) tuple."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            d = params["date"]
            # All days: foreign +, trust +, dealer +
            if d in ("20260326", "20260325", "20260324"):
                mock_resp.json.return_value = _make_t86_response(
                    ticker, net_buy=1_000_000, trust_buy=200_000, dealer_buy=50_000
                )
            elif d == "20260323":
                mock_resp.json.return_value = _make_t86_response(
                    ticker, net_buy=-100_000, trust_buy=-50_000, dealer_buy=-10_000
                )
            else:
                mock_resp.json.return_value = {"stat": "NO_DATA"}
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       side_effect=_side_effect):
                flags: list = []
                foreign_count, trust_count, dealer_count, _ = (
                    fetcher._fetch_institution_consecutive_days(ticker, trade_date, flags)
                )

        assert foreign_count == 3
        assert trust_count == 3
        assert dealer_count == 3

    def test_trust_consecutive_days_breaks_on_non_positive(self):
        """Trust consecutive day count resets when a day is zero or negative."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            d = params["date"]
            if d == "20260326":
                # Today: trust positive
                mock_resp.json.return_value = _make_t86_response(
                    ticker, net_buy=1_000_000, trust_buy=100_000, dealer_buy=50_000
                )
            elif d == "20260325":
                # Yesterday: trust ZERO → breaks streak
                mock_resp.json.return_value = _make_t86_response(
                    ticker, net_buy=500_000, trust_buy=0, dealer_buy=50_000
                )
            else:
                mock_resp.json.return_value = _make_t86_response(
                    ticker, net_buy=300_000, trust_buy=80_000, dealer_buy=30_000
                )
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       side_effect=_side_effect):
                flags: list = []
                _, trust_count, _, _ = fetcher._fetch_institution_consecutive_days(
                    ticker, trade_date, flags
                )

        # trust was 0 yesterday → streak is only 1 (today only)
        assert trust_count == 1

    def test_dealer_consecutive_days_zero_when_missing_column(self):
        """When T86 response has no 自營商買賣超股數 column, dealer count is 0."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        no_dealer_response = {
            "stat": "OK",
            "fields": ["證券代號", "外陸資買賣超股數", "投信買賣超股數"],
            "data": [[ticker, "+1,000,000", "+100,000"]],
        }

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = no_dealer_response
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       side_effect=_side_effect):
                flags: list = []
                foreign_count, trust_count, dealer_count, _ = (
                    fetcher._fetch_institution_consecutive_days(ticker, trade_date, flags)
                )

        assert foreign_count > 0
        assert trust_count > 0
        assert dealer_count == 0   # column absent → no dealer data → 0


# ------------------------------------------------------------------
# Tier A: _fetch_sbl_data
# ------------------------------------------------------------------

def _make_sbl_response(ticker: str, sbl_shares: int, total_shares: int) -> dict:
    """Minimal valid TWSE TWT93U SBL JSON response."""
    return {
        "stat": "OK",
        "fields": ["證券代號", "借券賣出成交股數", "當日成交股數"],
        "data": [
            [ticker, f"{sbl_shares:,}", f"{total_shares:,}"],
        ],
    }


class TestFetchSblData:
    def test_fetch_sbl_data_parses_correctly(self):
        """_fetch_sbl_data returns ratio = sbl_shares / total_shares."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        sbl_resp = MagicMock()
        sbl_resp.json.return_value = _make_sbl_response(ticker, sbl_shares=500_000, total_shares=5_000_000)
        sbl_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       return_value=sbl_resp):
                flags: list = []
                result = fetcher._fetch_sbl_data(ticker, trade_date, flags)

        assert result == pytest.approx(0.10)  # 500_000 / 5_000_000

    def test_fetch_sbl_data_returns_none_on_404(self):
        """_fetch_sbl_data returns None when HTTP error occurs."""
        import requests as req_module
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = req_module.exceptions.HTTPError("404")
                flags: list = []
                result = fetcher._fetch_sbl_data(ticker, trade_date, flags)

        assert result is None
        assert any("SBL_ERROR" in f for f in flags)

    def test_fetch_sbl_data_returns_none_on_schema_change(self):
        """_fetch_sbl_data returns None when required columns are missing."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        # Response with no SBL-related columns
        bad_schema_resp = MagicMock()
        bad_schema_resp.raise_for_status = MagicMock()
        bad_schema_resp.json.return_value = {
            "stat": "OK",
            "fields": ["證券代號", "成交金額"],  # no SBL columns at all
            "data": [[ticker, "1,000,000"]],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       return_value=bad_schema_resp):
                flags: list = []
                result = fetcher._fetch_sbl_data(ticker, trade_date, flags)

        assert result is None
        assert any("SBL_SCHEMA_CHANGED" in f for f in flags)


# ------------------------------------------------------------------
# Tier A: _fetch_margin_utilization
# ------------------------------------------------------------------

class TestFetchMarginUtilization:
    def test_fetch_margin_utilization_parses_credit_limit(self):
        """Returns balance/limit when 融資限額 is present in openapi response."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = _make_mi_margn_openapi_response(
            ticker, today_margin=20_000, prev_margin=18_000, margin_limit=100_000
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       return_value=resp):
                flags: list = []
                result = fetcher._fetch_margin_utilization(ticker, trade_date, flags)

        assert result == pytest.approx(0.20)  # 20_000 / 100_000

    def test_fetch_margin_utilization_returns_none_without_limit_column(self):
        """Returns None (no error flag) when 融資限額 field is empty string."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        # margin_limit=0 in helper → writes "" for 融資限額
        resp.json.return_value = _make_mi_margn_openapi_response(
            ticker, today_margin=20_000, prev_margin=18_000, margin_limit=0
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get",
                       return_value=resp):
                flags: list = []
                result = fetcher._fetch_margin_utilization(ticker, trade_date, flags)

        assert result is None
        # No error flag appended — absent limit is expected, not an error
        assert not any("MARGN" in f or "UTIL" in f for f in flags)
