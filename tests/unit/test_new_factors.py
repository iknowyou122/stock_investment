"""Unit tests for new factors: futures context, options PCR, valuation, revenue momentum."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher
from taiwan_stock_agent.domain.triple_confirmation_engine import (
    TripleConfirmationEngine,
)

TEST_DATE = date(2026, 6, 4)
TICKER = "2330"


def _make_fetcher(monkeypatch) -> PaidDataFetcher:
    monkeypatch.setenv("FINMIND_API_KEY", "test_key")
    return PaidDataFetcher()


def _mock_resp(data: list[dict]) -> MagicMock:
    r = MagicMock()
    r.json.return_value = {"data": data}
    r.raise_for_status = MagicMock()
    return r


# ── Futures Context ────────────────────────────────────────────────────────


class TestFetchFuturesContext:
    _TX_ROWS = [
        {"futures_id": "TX", "date": "2026-06-04", "institutional_investors": "外資",
         "long_open_interest_balance_volume": 15473, "short_open_interest_balance_volume": 82245},
        {"futures_id": "TX", "date": "2026-06-04", "institutional_investors": "投信",
         "long_open_interest_balance_volume": 56541, "short_open_interest_balance_volume": 5237},
        {"futures_id": "TE", "date": "2026-06-04", "institutional_investors": "外資",
         "long_open_interest_balance_volume": 100, "short_open_interest_balance_volume": 283},
    ]

    def test_parses_tx_foreign_net_oi(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._TX_ROWS)):
            ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["tx_foreign_net_oi"] == 15473 - 82245  # -66772

    def test_parses_tx_trust_net_oi(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._TX_ROWS)):
            ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["tx_trust_net_oi"] == 56541 - 5237  # +51304

    def test_parses_te_foreign_net_oi(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._TX_ROWS)):
            ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["te_foreign_net_oi"] == 100 - 283  # -183

    def test_composite_bearish_when_foreign_deeply_short(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._TX_ROWS)):
            ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["composite_bearish"] is True  # -66772 < -10000

    def test_composite_bullish_when_trust_long_and_foreign_not_deeply_short(self, monkeypatch):
        rows = [
            {"futures_id": "TX", "institutional_investors": "外資",
             "long_open_interest_balance_volume": 50000, "short_open_interest_balance_volume": 55000},
            {"futures_id": "TX", "institutional_investors": "投信",
             "long_open_interest_balance_volume": 30000, "short_open_interest_balance_volume": 5000},
        ]
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(rows)):
            ctx = pf.fetch_futures_context(TEST_DATE)
        # foreign_net = -5000 (> -30000), trust_net = +25000 → composite_bullish
        assert ctx["composite_bullish"] is True

    def test_caches_per_date(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._TX_ROWS)) as m:
            pf.fetch_futures_context(TEST_DATE)
            pf.fetch_futures_context(TEST_DATE)
        assert m.call_count == 1

    def test_no_key_returns_safe_fallback(self, monkeypatch):
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        pf = PaidDataFetcher()
        ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["data_available"] is False
        assert ctx["composite_bearish"] is False

    def test_api_failure_returns_safe_fallback(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", side_effect=Exception("timeout")):
            ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["data_available"] is False

    def test_data_available_true_on_success(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._TX_ROWS)):
            ctx = pf.fetch_futures_context(TEST_DATE)
        assert ctx["data_available"] is True


# ── Options PCR Context ────────────────────────────────────────────────────


class TestFetchOptionsContext:
    _TXO_ROWS = [
        {"option_id": "TXO", "institutional_investors": "外資", "call_put": "買權",
         "long_open_interest_balance_volume": 9531, "short_open_interest_balance_volume": 6864},
        {"option_id": "TXO", "institutional_investors": "外資", "call_put": "賣權",
         "long_open_interest_balance_volume": 23271, "short_open_interest_balance_volume": 16242},
        {"option_id": "TXO", "institutional_investors": "自營商", "call_put": "賣權",
         "long_open_interest_balance_volume": 31965, "short_open_interest_balance_volume": 28332},
    ]

    def _mock_options_resp(self, rows):
        return _mock_resp(rows)

    def test_parses_call_put_oi(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=self._mock_options_resp(self._TXO_ROWS)):
            ctx = pf.fetch_options_context(TEST_DATE)
        # foreign call_net = 9531 - 6864 = +2667
        # foreign put_net  = 23271 - 16242 = +7029
        assert ctx["foreign_call_net_oi"] == pytest.approx(2667)
        assert ctx["foreign_put_net_oi"] == pytest.approx(7029)

    def test_pcr_above_2_gives_strong_bearish_hedge(self, monkeypatch):
        # pcr = 7029 / 2667 ≈ 2.63
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=self._mock_options_resp(self._TXO_ROWS)):
            ctx = pf.fetch_options_context(TEST_DATE)
        assert ctx["signal"] == "STRONG_BEARISH_HEDGE"

    def test_pcr_below_0_8_gives_bullish_unwind(self, monkeypatch):
        rows = [
            {"option_id": "TXO", "institutional_investors": "外資", "call_put": "買權",
             "long_open_interest_balance_volume": 10000, "short_open_interest_balance_volume": 0},
            {"option_id": "TXO", "institutional_investors": "外資", "call_put": "賣權",
             "long_open_interest_balance_volume": 5000, "short_open_interest_balance_volume": 0},
        ]
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(rows)):
            ctx = pf.fetch_options_context(TEST_DATE)
        assert ctx["signal"] == "BULLISH_UNWIND"  # pcr = 5000/10000 = 0.5

    def test_zero_call_oi_is_none_safe(self, monkeypatch):
        rows = [
            {"option_id": "TXO", "institutional_investors": "外資", "call_put": "買權",
             "long_open_interest_balance_volume": 0, "short_open_interest_balance_volume": 0},
        ]
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(rows)):
            ctx = pf.fetch_options_context(TEST_DATE)
        assert ctx["pcr"] is None  # no ZeroDivisionError

    def test_caches_per_date(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=self._mock_options_resp(self._TXO_ROWS)) as m:
            pf.fetch_options_context(TEST_DATE)
            pf.fetch_options_context(TEST_DATE)
        assert m.call_count == 1

    def test_no_key_returns_safe_fallback(self, monkeypatch):
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        pf = PaidDataFetcher()
        ctx = pf.fetch_options_context(TEST_DATE)
        assert ctx["data_available"] is False
        assert ctx["signal"] == "NEUTRAL"

    def test_data_available_true_on_success(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=self._mock_options_resp(self._TXO_ROWS)):
            ctx = pf.fetch_options_context(TEST_DATE)
        assert ctx["data_available"] is True


# ── PER/Valuation Context ─────────────────────────────────────────────────


class TestFetchPerContext:
    _PER_ROW = {"date": "2026-06-04", "stock_id": "2330", "PER": 31.8, "PBR": 10.4, "dividend_yield": 0.93}

    def test_parses_per_pbr_dividend(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp([self._PER_ROW])):
            ctx = pf.fetch_per_context(TICKER, TEST_DATE)
        assert ctx["per"] == pytest.approx(31.8)
        assert ctx["pbr"] == pytest.approx(10.4)
        assert ctx["dividend_yield"] == pytest.approx(0.93)
        assert ctx["data_available"] is True

    def test_empty_data_returns_not_available(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp([])):
            ctx = pf.fetch_per_context(TICKER, TEST_DATE)
        assert ctx["data_available"] is False

    def test_caches_per_ticker_date(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp([self._PER_ROW])) as m:
            pf.fetch_per_context(TICKER, TEST_DATE)
            pf.fetch_per_context(TICKER, TEST_DATE)
        assert m.call_count == 1

    def test_different_tickers_cached_separately(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp([self._PER_ROW])) as m:
            pf.fetch_per_context("2330", TEST_DATE)
            pf.fetch_per_context("2454", TEST_DATE)
        assert m.call_count == 2

    def test_no_key_returns_not_available(self, monkeypatch):
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        pf = PaidDataFetcher()
        ctx = pf.fetch_per_context(TICKER, TEST_DATE)
        assert ctx["data_available"] is False


# ── Revenue Momentum Context ───────────────────────────────────────────────


class TestFetchRevenueContext:
    def _make_revenue_rows(self) -> list[dict]:
        """14 months of revenue data for YoY computation."""
        rows = []
        # 2025: month 5-12 (base year)
        base = {5: 100, 6: 110, 7: 120, 8: 130, 9: 140, 10: 150, 11: 160, 12: 170}
        for m, v in base.items():
            rows.append({"revenue_year": 2025, "revenue_month": m, "revenue": v * 1_000_000})
        # 2026: month 1-5 (current year, growing >20% YoY)
        current = {1: 145, 2: 155, 3: 165, 4: 175, 5: 130}
        for m, v in current.items():
            rows.append({"revenue_year": 2026, "revenue_month": m, "revenue": v * 1_000_000})
        return rows

    def test_computes_yoy_growth(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._make_revenue_rows())):
            ctx = pf.fetch_revenue_context(TICKER, TEST_DATE)
        # Most recent month: 2026-05 vs 2025-05: (130/100 - 1)*100 = 30%
        assert ctx["data_available"] is True
        assert ctx["yoy_growth"] == pytest.approx(30.0)

    def test_consecutive_positive_yoy_counted(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._make_revenue_rows())):
            ctx = pf.fetch_revenue_context(TICKER, TEST_DATE)
        assert ctx["consecutive_positive_yoy"] >= 1

    def test_insufficient_data_returns_not_available(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp([
            {"revenue_year": 2026, "revenue_month": 5, "revenue": 100_000_000}
        ])):
            ctx = pf.fetch_revenue_context(TICKER, TEST_DATE)
        assert ctx["data_available"] is False

    def test_caches_per_ticker_date(self, monkeypatch):
        pf = _make_fetcher(monkeypatch)
        with patch("requests.get", return_value=_mock_resp(self._make_revenue_rows())) as m:
            pf.fetch_revenue_context(TICKER, TEST_DATE)
            pf.fetch_revenue_context(TICKER, TEST_DATE)
        assert m.call_count == 1


# ── TCE Static Scoring Methods ─────────────────────────────────────────────


class TestTCEStaticScoringMethods:
    """Test new static scoring methods directly on TripleConfirmationEngine."""

    def test_score_valuation_value_zone(self):
        pts, flags = TripleConfirmationEngine._score_valuation(
            {"per": 12.0, "pbr": 1.2, "dividend_yield": 0.5, "data_available": True}
        )
        assert pts == pytest.approx(3.0)
        assert "VALUE_ZONE" in flags

    def test_score_valuation_high_yield(self):
        pts, flags = TripleConfirmationEngine._score_valuation(
            {"per": 20.0, "pbr": 2.0, "dividend_yield": 4.0, "data_available": True}
        )
        assert pts == pytest.approx(2.0)
        assert "DIVIDEND_SUPPORT" in flags

    def test_score_valuation_high_yield_5pct(self):
        pts, flags = TripleConfirmationEngine._score_valuation(
            {"per": 20.0, "pbr": 2.0, "dividend_yield": 6.0, "data_available": True}
        )
        assert pts == pytest.approx(3.0)
        assert "HIGH_YIELD_SUPPORT" in flags

    def test_score_valuation_expensive_per(self):
        pts, flags = TripleConfirmationEngine._score_valuation(
            {"per": 55.0, "pbr": 10.0, "dividend_yield": 0.5, "data_available": True}
        )
        assert pts == pytest.approx(-2.0)
        assert "EXPENSIVE_PER" in flags

    def test_score_valuation_clamped_max(self):
        # per < 15 (+3) + yield > 5% (+3) = 6, clamped to 5
        pts, _ = TripleConfirmationEngine._score_valuation(
            {"per": 10.0, "pbr": 1.0, "dividend_yield": 5.5, "data_available": True}
        )
        assert pts == pytest.approx(5.0)

    def test_score_valuation_none_per_skipped(self):
        pts, flags = TripleConfirmationEngine._score_valuation(
            {"per": None, "pbr": None, "dividend_yield": 1.0, "data_available": True}
        )
        assert pts == pytest.approx(0.0)
        assert "VALUE_ZONE" not in flags

    def test_score_revenue_momentum_high_growth(self):
        pts, flags = TripleConfirmationEngine._score_revenue_momentum(
            {"yoy_growth": 55.0, "consecutive_positive_yoy": 3, "data_available": True}
        )
        assert pts == pytest.approx(7.0)  # 5 + 2, capped at 7
        assert any("REVENUE_SURGE" in f for f in flags)
        assert "REVENUE_CONSISTENT" in flags

    def test_score_revenue_momentum_moderate_growth(self):
        pts, flags = TripleConfirmationEngine._score_revenue_momentum(
            {"yoy_growth": 25.0, "consecutive_positive_yoy": 1, "data_available": True}
        )
        assert pts == pytest.approx(3.0)
        assert any("REVENUE_GROWTH" in f for f in flags)

    def test_score_revenue_momentum_no_growth(self):
        pts, flags = TripleConfirmationEngine._score_revenue_momentum(
            {"yoy_growth": 10.0, "consecutive_positive_yoy": 0, "data_available": True}
        )
        assert pts == pytest.approx(0.0)
        assert flags == []

    def test_score_market_regime_bullish_bonus(self):
        ctx = {
            "futures_ctx": {"tx_foreign_net_oi": -5000, "tx_trust_net_oi": 20000, "data_available": True},
            "options_ctx": {"pcr": 0.6, "signal": "BULLISH_UNWIND", "data_available": True},
        }
        pts, flags = TripleConfirmationEngine._score_market_regime(ctx)
        assert pts == pytest.approx(3.0)
        assert "MKTOPT_BULLISH" in flags

    def test_score_market_regime_bearish_no_bonus(self):
        ctx = {
            "futures_ctx": {"tx_foreign_net_oi": -66772, "tx_trust_net_oi": 51304, "data_available": True},
            "options_ctx": {"pcr": 2.63, "signal": "STRONG_BEARISH_HEDGE", "data_available": True},
        }
        pts, flags = TripleConfirmationEngine._score_market_regime(ctx)
        assert pts == pytest.approx(0.0)  # no bullish bonus
        assert "MKTOPT_STRONG_BEARISH_HEDGE" in flags

    def test_score_market_regime_smart_diverge_flag(self):
        ctx = {
            "futures_ctx": {"tx_foreign_net_oi": -50000, "tx_trust_net_oi": 30000, "data_available": True},
            "options_ctx": {"pcr": 1.5, "signal": "BEARISH", "data_available": True},
        }
        _, flags = TripleConfirmationEngine._score_market_regime(ctx)
        assert "FUTURES_SMART_DIVERGE" in flags

    def test_score_market_regime_no_data_returns_zero(self):
        ctx = {}
        pts, flags = TripleConfirmationEngine._score_market_regime(ctx)
        assert pts == pytest.approx(0.0)
        assert flags == []

    def test_new_fields_default_to_zero_in_breakdown(self):
        """_ScoreBreakdown new fields default to 0, don't affect existing total."""
        from taiwan_stock_agent.domain.triple_confirmation_engine import _ScoreBreakdown
        bd = _ScoreBreakdown()
        assert bd.market_regime_bonus_pts == pytest.approx(0.0)
        assert bd.valuation_pts == pytest.approx(0.0)
        assert bd.revenue_momentum_pts == pytest.approx(0.0)

    def test_valuation_pts_increase_total(self):
        """valuation_pts added to p4 → total increases."""
        from taiwan_stock_agent.domain.triple_confirmation_engine import _ScoreBreakdown
        bd = _ScoreBreakdown()
        base_total = bd.total
        bd.valuation_pts = 3.0
        assert bd.total == pytest.approx(base_total + 3.0)

    def test_revenue_momentum_pts_increase_total(self):
        from taiwan_stock_agent.domain.triple_confirmation_engine import _ScoreBreakdown
        bd = _ScoreBreakdown()
        base_total = bd.total
        bd.revenue_momentum_pts = 5.0
        assert bd.total == pytest.approx(base_total + 5.0)

    def test_market_regime_bonus_pts_increase_total(self):
        from taiwan_stock_agent.domain.triple_confirmation_engine import _ScoreBreakdown
        bd = _ScoreBreakdown()
        base_total = bd.total
        bd.market_regime_bonus_pts = 3.0
        assert bd.total == pytest.approx(base_total + 3.0)
