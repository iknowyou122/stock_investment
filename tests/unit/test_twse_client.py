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


def _make_mi_margn_response(ticker: str, balance: int) -> dict:
    """Minimal valid TWSE MI_MARGN JSON response."""
    return {
        "stat": "OK",
        "fields": ["股票代號", "融資餘額"],
        "data": [
            [ticker, f"{balance:,}"],
        ],
    }


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

        margn_today = MagicMock()
        margn_today.json.return_value = _make_mi_margn_response(ticker, 10_000)
        margn_today.raise_for_status = MagicMock()

        margn_prev = MagicMock()
        margn_prev.json.return_value = _make_mi_margn_response(ticker, 12_000)
        margn_prev.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                # First call = T86, second = today's margin, third = yesterday's margin
                mock_get.side_effect = [t86_resp, margn_today, margn_prev]
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
        """T86 returns data, MI_MARGN returns empty → partial proxy with flag."""
        ticker = "2330"
        trade_date = date(2026, 3, 24)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response(ticker, net_buy=3_000_000, trust_buy=100_000)
        t86_resp.raise_for_status = MagicMock()

        # MI_MARGN returns no data
        margn_resp = MagicMock()
        margn_resp.json.return_value = {"stat": "OK", "data": [], "fields": ["股票代號", "融資餘額"]}
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = [t86_resp, margn_resp]
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

        # MI_MARGN returns no data — only T86 matters here
        margn_resp = MagicMock()
        margn_resp.json.return_value = {"stat": "OK", "data": [], "fields": ["股票代號", "融資餘額"]}
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = [t86_resp, margn_resp]
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
        margn_resp.json.return_value = {"stat": "OK", "data": [], "fields": ["股票代號", "融資餘額"]}
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = [t86_resp, margn_resp]
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
        margn_resp.json.return_value = {"stat": "OK", "data": [], "fields": ["股票代號", "融資餘額"]}
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = [t86_resp, margn_resp]
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
        margn_resp.json.return_value = {"stat": "OK", "data": [], "fields": ["股票代號", "融資餘額"]}
        margn_resp.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))

            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get") as mock_get:
                mock_get.side_effect = [t86_resp, margn_resp]
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

            # Margin caches (today + yesterday for margin_balance_change)
            pd.DataFrame([{"margin_balance": 8_000}]).to_parquet(
                Path(tmpdir) / f"twse_margin_{ticker}_{trade_date}.parquet", index=False
            )
            pd.DataFrame([{"margin_balance": 10_000}]).to_parquet(
                Path(tmpdir) / f"twse_margin_{ticker}_{trade_date - timedelta(days=1)}.parquet", index=False
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

            # Short balance caches (today + yesterday for short_balance_increased)
            pd.DataFrame([{"short_balance": 5_000}]).to_parquet(
                Path(tmpdir) / f"twse_short_{ticker}_{trade_date}.parquet", index=False
            )
            pd.DataFrame([{"short_balance": 4_000}]).to_parquet(
                Path(tmpdir) / f"twse_short_{ticker}_{trade_date - timedelta(days=1)}.parquet", index=False
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
        assert proxy.short_balance_increased is True          # 5_000 > 4_000 * 1.20
        assert proxy.short_margin_ratio == pytest.approx(5_000 / 8_000)


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

def _make_mi_margn_full_response(ticker: str, margin: int, short: int) -> dict:
    """Minimal MI_MARGN response with both 融資餘額 and 融券餘額."""
    return {
        "stat": "OK",
        "fields": ["股票代號", "融資餘額", "融券餘額"],
        "data": [
            [ticker, f"{margin:,}", f"{short:,}"],
        ],
    }


class TestShortBalanceData:
    def test_short_spike_detected(self):
        """When today's 融券餘額 > yesterday's × 1.20, short_balance_increased is True."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
        t86_resp.raise_for_status = MagicMock()

        margn_today = MagicMock()
        margn_today.json.return_value = _make_mi_margn_full_response(ticker, margin=10_000, short=6_000)
        margn_today.raise_for_status = MagicMock()

        margn_prev = MagicMock()
        margn_prev.json.return_value = _make_mi_margn_full_response(ticker, margin=10_000, short=4_000)
        margn_prev.raise_for_status = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            # T86 (today) + many lookback T86s + margin (today) + margin (prev)
            # + short (today) + short (prev)
            # We use a mapping approach to avoid ordering fragility
            def _short_side_effect(url, params, **kwargs):
                if "T86" in url:
                    return t86_resp
                d = params["date"]
                if d == trade_date.strftime("%Y%m%d"):
                    return margn_today
                return margn_prev

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

        t86_resp = MagicMock()
        t86_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
        t86_resp.raise_for_status = MagicMock()

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            if "T86" in url:
                mock_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
            else:
                d = params["date"]
                if d == trade_date.strftime("%Y%m%d"):
                    mock_resp.json.return_value = _make_mi_margn_full_response(ticker, margin=10_000, short=4_500)
                else:
                    mock_resp.json.return_value = _make_mi_margn_full_response(ticker, margin=10_000, short=4_000)
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        # 4_500 > 4_000 * 1.20 = 4_800 → False
        assert proxy.short_balance_increased is False
        assert proxy.short_margin_ratio == pytest.approx(4_500 / 10_000)

    def test_short_data_unavailable_returns_defaults(self):
        """When MI_MARGN returns no 融券餘額 column, short fields default to safe values."""
        ticker = "2330"
        trade_date = date(2026, 3, 26)

        def _side_effect(url, params, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            if "T86" in url:
                mock_resp.json.return_value = _make_t86_response("2330", net_buy=1_000_000)
            else:
                # MI_MARGN without 融券餘額 column
                mock_resp.json.return_value = {
                    "stat": "OK",
                    "fields": ["股票代號", "融資餘額"],
                    "data": [[ticker, "10,000"]],
                }
            return mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher = ChipProxyFetcher(cache_dir=Path(tmpdir))
            with patch("taiwan_stock_agent.infrastructure.twse_client.requests.get", side_effect=_side_effect):
                proxy = fetcher.fetch(ticker, trade_date)

        assert proxy.short_balance_increased is False
        assert proxy.short_margin_ratio == 0.0
