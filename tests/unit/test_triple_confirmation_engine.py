"""Unit tests for TripleConfirmationEngine.

Coverage targets (from test diagram in design doc):
  - Pillar 1: vwap_5d (price above / below)
  - Pillar 1: volume surge (above / below 1.5x threshold)
  - Pillar 2: net_buyer_count_diff > 0 / <= 0
  - Pillar 2: concentration_top15 > 0.35 / <= 0.35
  - Pillar 2: no_daytrade_top3 (quality bonus / 隔日沖 deduction)
  - Pillar 3: space proxy (close > twenty_day_high * 0.99)
  - Action mapping: LONG / WATCH / CAUTION thresholds
  - Execution plan: entry/stop/target formulas
  - Thin market guard (active_branch_count < 10)
  - Insufficient history data quality flag
  - score clamp: max(0, min(100))
  # New (enhance-analysis-factors plan):
  - TWSE free-tier proxy scoring (外資+融資)
  - Paid vs free-tier mutual exclusion
  - MA20 slope scoring (+5 pts)
  - MA20 slope insufficient history (< 24 sessions)
  - _AnalysisHints isolation (never affects total)
  - free_tier_mode threshold switching (55 vs 70)
  - LONG guard (chip_pts=0 + free_tier_mode → WATCH)
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.models import (
    BrokerWithLabel,
    ChipReport,
    DailyOHLCV,
    TWSEChipProxy,
    VolumeProfile,
)
from taiwan_stock_agent.domain.triple_confirmation_engine import (
    TripleConfirmationEngine,
    _AnalysisHints,
    _ScoreBreakdown,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_history(n: int, base_close: float = 100.0, base_vol: int = 10_000) -> list[DailyOHLCV]:
    """Generate n days of ascending OHLCV."""
    result = []
    d = date(2025, 1, 2)
    for i in range(n):
        close = base_close + i * 0.1
        result.append(
            DailyOHLCV(
                ticker="9999",
                trade_date=d + timedelta(days=i),
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=base_vol,
            )
        )
    return result


def _make_chip_report(
    *,
    net_buyer_count_diff: int = 5,
    concentration_top15: float = 0.40,
    active_branch_count: int = 20,
    top3_has_daytrade: bool = False,
) -> ChipReport:
    label = "隔日沖" if top3_has_daytrade else "unknown"
    top_buyers = [
        BrokerWithLabel(
            branch_code="A001",
            branch_name="元大-板橋",
            label=label,
            reversal_rate=0.7 if top3_has_daytrade else 0.3,
            buy_volume=50_000,
            sell_volume=5_000,
        ),
        BrokerWithLabel(
            branch_code="B002",
            branch_name="富邦-台北",
            label="unknown",
            reversal_rate=0.3,
            buy_volume=30_000,
            sell_volume=0,
        ),
        BrokerWithLabel(
            branch_code="C003",
            branch_name="國泰-信義",
            label="unknown",
            reversal_rate=0.3,
            buy_volume=10_000,
            sell_volume=0,
        ),
    ]
    return ChipReport(
        ticker="9999",
        report_date=date(2025, 1, 31),
        top_buyers=top_buyers,
        concentration_top15=concentration_top15,
        net_buyer_count_diff=net_buyer_count_diff,
        risk_flags=[],
        active_branch_count=active_branch_count,
    )


def _make_volume_profile(twenty_day_high: float = 115.0) -> VolumeProfile:
    return VolumeProfile(
        ticker="9999",
        period_end=date(2025, 1, 31),
        poc_proxy=twenty_day_high,
        twenty_day_high=twenty_day_high,
        twenty_day_sessions=20,
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestPillar1Momentum:
    def test_vwap_5d_above_gets_20_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0, base_vol=10_000)
        # Last close is ~102.4 — above 5-day vwap (all close together)
        ohlcv = history[-1]
        # Force close clearly above vwap by making last close highest
        ohlcv = ohlcv.model_copy(update={"close": 110.0})
        chip = _make_chip_report()
        vp = _make_volume_profile(twenty_day_high=108.0)
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.vwap_5d_pts == 20

    def test_vwap_5d_below_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0, base_vol=10_000)
        ohlcv = history[-1].model_copy(update={"close": 90.0})  # well below history
        chip = _make_chip_report()
        vp = _make_volume_profile()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.vwap_5d_pts == 0

    def test_volume_surge_above_15x_gets_20_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_vol=10_000)
        ohlcv = history[-1].model_copy(update={"volume": 20_000})  # 2x > 1.5x threshold
        chip = _make_chip_report()
        vp = _make_volume_profile()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.volume_surge_pts == 20

    def test_volume_no_surge_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_vol=10_000)
        ohlcv = history[-1].model_copy(update={"volume": 11_000})  # 1.1x < 1.5x
        chip = _make_chip_report()
        vp = _make_volume_profile()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.volume_surge_pts == 0


class TestPillar2Chip:
    def test_positive_net_buyer_count_diff_gets_15_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(net_buyer_count_diff=3)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.net_buyer_diff_pts == 15

    def test_zero_net_buyer_count_diff_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(net_buyer_count_diff=0)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.net_buyer_diff_pts == 0

    def test_negative_net_buyer_count_diff_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(net_buyer_count_diff=-2)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.net_buyer_diff_pts == 0

    def test_concentration_above_35pct_gets_15_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(concentration_top15=0.50)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.concentration_pts == 15

    def test_concentration_below_35pct_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(concentration_top15=0.20)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.concentration_pts == 0

    def test_thin_market_skips_concentration_check(self):
        """Concentration check skipped when active_branch_count < 10."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(concentration_top15=0.99, active_branch_count=5)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        # Even though concentration is 99%, thin market guard should skip
        assert bd.concentration_pts == 0
        assert any("THIN_MARKET" in f for f in bd.flags)

    def test_no_daytrade_in_top3_gets_10_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(top3_has_daytrade=False)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.no_daytrade_pts == 10
        assert bd.daytrade_deduction == 0

    def test_daytrade_in_top3_loses_points_and_adds_deduction(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report(top3_has_daytrade=True)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile())
        assert bd.no_daytrade_pts == 0
        assert bd.daytrade_deduction == 25
        assert any("隔日沖_TOP3" in f for f in bd.flags)


class TestPillar3Space:
    def test_close_at_20d_high_gets_20_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0)
        ohlcv = history[-1].model_copy(update={"close": 115.0})
        chip = _make_chip_report()
        vp = _make_volume_profile(twenty_day_high=115.0)
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.space_pts == 20

    def test_close_slightly_below_20d_high_gets_20_pts(self):
        """Within 1% of 20-day high should still qualify."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0)
        twenty_day_high = 115.0
        ohlcv = history[-1].model_copy(update={"close": twenty_day_high * 0.995})
        chip = _make_chip_report()
        vp = _make_volume_profile(twenty_day_high=twenty_day_high)
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.space_pts == 20

    def test_close_below_99pct_of_20d_high_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0)
        twenty_day_high = 115.0
        ohlcv = history[-1].model_copy(update={"close": twenty_day_high * 0.95})
        chip = _make_chip_report()
        vp = _make_volume_profile(twenty_day_high=twenty_day_high)
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert bd.space_pts == 0


class TestActionMapping:
    def test_confidence_70_maps_to_long(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(70) == "LONG"

    def test_confidence_100_maps_to_long(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(100) == "LONG"

    def test_confidence_30_maps_to_caution(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(30) == "CAUTION"

    def test_confidence_0_maps_to_caution(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(0) == "CAUTION"

    def test_confidence_50_maps_to_watch(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(50) == "WATCH"

    def test_confidence_31_maps_to_watch(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(31) == "WATCH"

    def test_confidence_69_maps_to_watch(self):
        engine = TripleConfirmationEngine()
        assert engine._map_action(69) == "WATCH"


class TestExecutionPlan:
    def test_entry_and_target_formulas(self):
        engine = TripleConfirmationEngine()
        from taiwan_stock_agent.domain.models import DailyOHLCV, VolumeProfile

        ohlcv = DailyOHLCV(
            ticker="2330",
            trade_date=date(2025, 1, 31),
            open=985.0, high=995.0, low=980.0, close=990.0, volume=50_000,
        )
        vp = VolumeProfile(
            ticker="2330",
            period_end=date(2025, 1, 31),
            poc_proxy=1000.0,
            twenty_day_high=1000.0,
            twenty_day_sessions=20,
        )
        plan = engine._make_execution_plan(ohlcv, vp)
        assert plan.entry_bid_limit == round(990.0 * 0.995, 2)
        assert plan.entry_max_chase == round(990.0 * 1.005, 2)
        assert plan.stop_loss == round(990.0, 2)
        assert plan.target == round(1000.0 * 1.05, 2)


class TestScoreClamp:
    def test_score_never_exceeds_100(self):
        """All pillars max + no risk deductions = 100, not more."""
        engine = TripleConfirmationEngine()
        # Build history where all pillars fire
        history = _make_history(25, base_close=100.0, base_vol=10_000)
        # Close is well above vwap_5d; volume 3x avg; near 20d high
        ohlcv = DailyOHLCV(
            ticker="9999",
            trade_date=history[-1].trade_date,
            open=120.0, high=122.0, low=119.0, close=121.0, volume=30_000,
        )
        chip = _make_chip_report(
            net_buyer_count_diff=10,
            concentration_top15=0.60,
            active_branch_count=20,
            top3_has_daytrade=False,
        )
        vp = _make_volume_profile(twenty_day_high=120.0)
        signal, bd = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert signal.confidence <= 100
        assert signal.confidence >= 0

    def test_score_never_below_0(self):
        """Max risk deductions should clamp to 0."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0, base_vol=10_000)
        ohlcv = history[-1].model_copy(update={"close": 50.0, "volume": 1_000})
        chip = _make_chip_report(
            net_buyer_count_diff=-10,
            concentration_top15=0.05,
            active_branch_count=20,
            top3_has_daytrade=True,  # -25 pts
        )
        vp = _make_volume_profile(twenty_day_high=200.0)
        signal, _ = engine.score_with_breakdown(ohlcv, history, chip, vp)
        assert signal.confidence >= 0


class TestDataQualityFlags:
    def test_insufficient_history_propagated(self):
        """Data quality flags from OHLCV, chip report, and volume profile merge."""
        engine = TripleConfirmationEngine()
        history = _make_history(5)  # only 5 sessions
        ohlcv = history[-1]
        ohlcv.data_quality_flags.append("INSUFFICIENT_HISTORY: 5 sessions (need 20)")
        chip = _make_chip_report()
        chip.data_quality_flags.append("PARTIAL_HISTORY: 2 days")
        vp = _make_volume_profile()
        vp.data_quality_flags.append("PARTIAL_PROFILE: only 5 sessions")

        signal, _ = engine.score_with_breakdown(ohlcv, history, chip, vp)
        flags = signal.data_quality_flags
        assert any("INSUFFICIENT_HISTORY" in f for f in flags)
        assert any("PARTIAL_HISTORY" in f for f in flags)
        assert any("PARTIAL_PROFILE" in f for f in flags)


# ------------------------------------------------------------------
# New tests: enhance-analysis-factors plan
# ------------------------------------------------------------------

def _make_empty_chip_report() -> ChipReport:
    """Chip report with zero counts — forces free-tier path."""
    return ChipReport(
        ticker="9999",
        report_date=date(2025, 1, 31),
        top_buyers=[],
        concentration_top15=0.0,
        net_buyer_count_diff=0,
        risk_flags=[],
        active_branch_count=0,
    )


def _make_twse_proxy(
    *,
    foreign_net_buy: int = 0,
    margin_balance_change: int = 0,
    is_available: bool = True,
) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="9999",
        trade_date=date(2025, 1, 31),
        foreign_net_buy=foreign_net_buy,
        margin_balance_change=margin_balance_change,
        is_available=is_available,
    )


class TestTWSEFreeChipScoring:
    def test_foreign_net_buy_positive_gets_15_pts(self):
        """外資買賣超 > 0 → +15 pts on twse_foreign_pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=1_000_000, margin_balance_change=0)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_pts == 15

    def test_foreign_net_buy_zero_gets_0_pts(self):
        """外資買賣超 = 0 → +0 pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=0, margin_balance_change=0)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_pts == 0

    def test_margin_balance_decreasing_gets_10_pts(self):
        """融資餘額 change ≤ 0 → +10 pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=0, margin_balance_change=-500)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_margin_pts == 10

    def test_margin_balance_zero_change_gets_10_pts(self):
        """融資餘額 change = 0 → +10 pts (≤ 0 condition)."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=0, margin_balance_change=0)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_margin_pts == 10

    def test_margin_balance_increasing_gets_0_pts(self):
        """融資餘額 change > 0 → +0 pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=0, margin_balance_change=1_000)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_margin_pts == 0

    def test_both_twse_proxies_pass_gets_25_pts(self):
        """Both 外資 > 0 and 融資 ≤ 0 → +25 total chip pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=2_000_000, margin_balance_change=-1_000)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_pts == 15
        assert bd.twse_margin_pts == 10
        assert bd.chip_pts == 25


class TestPaidTierExcludesTWSE:
    def test_paid_chip_data_skips_twse_proxies(self):
        """When FinMind paid chip data is available, TWSE proxies are ignored."""
        engine = TripleConfirmationEngine(free_tier_mode=False)
        history = _make_history(25)
        # Paid chip with positive net_buyer_count_diff triggers paid path
        chip = _make_chip_report(net_buyer_count_diff=5)
        proxy = _make_twse_proxy(foreign_net_buy=9_999_999, margin_balance_change=-9_999_999)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        # TWSE pts must be 0 when paid chip is in effect
        assert bd.twse_foreign_pts == 0
        assert bd.twse_margin_pts == 0
        # Paid pts should be scored instead
        assert bd.net_buyer_diff_pts == 15


class TestMA20SlopeScoring:
    def _make_rising_history(self, n: int = 30) -> list[DailyOHLCV]:
        """History with steadily rising close prices (positive MA20 slope)."""
        result = []
        d = date(2025, 1, 2)
        for i in range(n):
            close = 100.0 + i * 0.5  # each day +0.5
            result.append(
                DailyOHLCV(
                    ticker="9999",
                    trade_date=d + date.resolution * i,
                    open=close - 0.2,
                    high=close + 0.5,
                    low=close - 0.5,
                    close=close,
                    volume=10_000,
                )
            )
        return result

    def _make_falling_history(self, n: int = 30) -> list[DailyOHLCV]:
        """History with steadily falling close prices (negative MA20 slope)."""
        result = []
        d = date(2025, 1, 2)
        for i in range(n):
            close = 200.0 - i * 0.5  # each day -0.5
            result.append(
                DailyOHLCV(
                    ticker="9999",
                    trade_date=d + date.resolution * i,
                    open=close + 0.2,
                    high=close + 0.5,
                    low=close - 0.5,
                    close=close,
                    volume=10_000,
                )
            )
        return result

    def test_rising_ma20_gets_5_pts(self):
        engine = TripleConfirmationEngine()
        history = self._make_rising_history(30)
        chip = _make_chip_report()
        vp = _make_volume_profile(twenty_day_high=history[-1].close * 1.01)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, vp)
        assert bd.ma20_slope_pts == 5

    def test_falling_ma20_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = self._make_falling_history(30)
        chip = _make_chip_report()
        vp = _make_volume_profile(twenty_day_high=200.0)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, vp)
        assert bd.ma20_slope_pts == 0

    def test_insufficient_history_gets_0_pts_and_flag(self):
        """< 25 sessions → slope = None → +0 pts + INSUFFICIENT_HISTORY_MA20_SLOPE flag."""
        engine = TripleConfirmationEngine()
        history = _make_history(24)  # one short of minimum (need 25)
        chip = _make_chip_report()
        vp = _make_volume_profile()
        _, bd = engine.score_with_breakdown(history[-1], history, chip, vp)
        assert bd.ma20_slope_pts == 0
        assert "INSUFFICIENT_HISTORY_MA20_SLOPE" in bd.flags

    def test_exactly_25_sessions_computes_slope(self):
        """Exactly 25 sessions is the minimum: rolling(20) at iloc[-6] is valid."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0)  # flat → slope ≈ 0
        chip = _make_chip_report()
        vp = _make_volume_profile()
        _, bd = engine.score_with_breakdown(history[-1], history, chip, vp)
        # No flag for insufficient history — slope was computable (even if 0)
        assert "INSUFFICIENT_HISTORY_MA20_SLOPE" not in bd.flags


class TestAnalysisHintsIsolation:
    def test_hints_never_in_score(self):
        """_AnalysisHints fields must not affect _ScoreBreakdown.total."""
        engine = TripleConfirmationEngine()
        history = _make_history(30)
        chip = _make_chip_report()
        vp = _make_volume_profile()

        signal_full, bd, hints = engine.score_full(history[-1], history, chip, vp)
        signal_plain, bd2 = engine.score_with_breakdown(history[-1], history, chip, vp)

        # Scores must be identical regardless of hints
        assert signal_full.confidence == signal_plain.confidence
        assert bd.total == bd2.total

    def test_hints_object_is_separate_from_breakdown(self):
        """score_full returns distinct objects; hints fields absent from breakdown."""
        engine = TripleConfirmationEngine()
        history = _make_history(30)
        chip = _make_chip_report()
        vp = _make_volume_profile()

        _, bd, hints = engine.score_full(history[-1], history, chip, vp)

        # Ensure _AnalysisHints fields do not exist on _ScoreBreakdown
        assert not hasattr(bd, "rsi_14")
        assert not hasattr(bd, "macd_line")
        assert not hasattr(bd, "macd_signal")
        assert not hasattr(bd, "ma20_streak")
        assert not hasattr(bd, "gap_down_pct")

        # hints is a proper _AnalysisHints instance
        assert isinstance(hints, _AnalysisHints)

    def test_all_hints_none_does_not_error(self):
        """If history is too short for any hint, all hints are None — no exception."""
        engine = TripleConfirmationEngine()
        history = _make_history(5)  # insufficient for RSI, MACD, MA20
        chip = _make_chip_report()
        vp = _make_volume_profile()

        _, _, hints = engine.score_full(history[-1], history, chip, vp)
        # Should not raise and hints should be all None
        assert hints.rsi_14 is None
        assert hints.macd_line is None
        assert hints.macd_signal is None


class TestFreeTierThreshold:
    def test_free_tier_mode_threshold_is_55(self):
        """free_tier_mode=True lowers LONG threshold to 55."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        # Score of 60 with chip data → LONG in free tier (60 >= 55)
        assert engine._map_action(60, chip_pts=15) == "LONG"

    def test_paid_tier_threshold_is_70(self):
        """free_tier_mode=False (default) keeps LONG threshold at 70."""
        engine = TripleConfirmationEngine(free_tier_mode=False)
        # Score of 60 without free tier → WATCH (60 < 70)
        assert engine._map_action(60) == "WATCH"
        # Score of 70 → LONG
        assert engine._map_action(70) == "LONG"

    def test_free_tier_signal_output_has_free_tier_true(self):
        """SignalOutput.free_tier_mode == True when engine is in free tier mode."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(foreign_net_buy=1_000_000, margin_balance_change=-500)
        signal = engine.score(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert signal.free_tier_mode is True

    def test_paid_tier_signal_output_has_free_tier_none(self):
        """SignalOutput.free_tier_mode == None (legacy) for default engine."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        chip = _make_chip_report()
        signal = engine.score(history[-1], history, chip, _make_volume_profile())
        assert signal.free_tier_mode is None


class TestLONGGuard:
    def test_long_blocked_when_chip_pts_zero_in_free_tier(self):
        """LONG guard: chip_pts=0 + free_tier_mode=True → WATCH, not LONG."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        # Score above free-tier threshold (55) but chip_pts=0
        assert engine._map_action(60, chip_pts=0) == "WATCH"

    def test_long_allowed_when_chip_pts_present_in_free_tier(self):
        """LONG guard bypassed when chip_pts > 0 in free tier mode."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        assert engine._map_action(60, chip_pts=15) == "LONG"

    def test_long_guard_end_to_end(self):
        """Integration: TWSE unavailable in free_tier_mode → signal is WATCH."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25, base_vol=10_000)
        # Build scenario where Pillar1+3 = 60 pts, no chip data
        ohlcv = history[-1].model_copy(update={"close": 150.0, "volume": 20_000})
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(is_available=False)  # TWSE unavailable
        vp = _make_volume_profile(twenty_day_high=150.0)

        signal = engine.score(ohlcv, history, chip, vp, twse_proxy=proxy)
        # Even if score >= 55, LONG should be blocked
        assert signal.action in ("WATCH", "CAUTION")
        assert signal.action != "LONG"


class TestSignalOutputFreeTierTristate:
    def test_none_legacy_callers(self):
        """free_tier_mode=None preserved for backward-compatible callers."""
        engine = TripleConfirmationEngine()  # default, no free_tier_mode
        history = _make_history(25)
        chip = _make_chip_report()
        signal = engine.score(history[-1], history, chip, _make_volume_profile())
        assert signal.free_tier_mode is None

    def test_true_free_tier(self):
        """free_tier_mode=True set when engine is free_tier_mode=True."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy(is_available=True)
        signal = engine.score(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert signal.free_tier_mode is True

    def test_false_not_set(self):
        """free_tier_mode=False (paid) results in None (not False) — by design."""
        # Design decision #10: None=legacy callers unaffected; True=free-tier explicit
        engine = TripleConfirmationEngine(free_tier_mode=False)
        history = _make_history(25)
        chip = _make_chip_report()
        signal = engine.score(history[-1], history, chip, _make_volume_profile())
        # paid mode → None (not False) for backward compat
        assert signal.free_tier_mode is None


# ------------------------------------------------------------------
# Factor 1: K線收盤強弱比 (close_strength_pts)
# ------------------------------------------------------------------

class TestCloseStrengthScore:
    """Factor 1: (close - low) / (high - low) > 0.7 → +5 pts."""

    def _run(self, close: float, high: float, low: float) -> _ScoreBreakdown:
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": close, "high": high, "low": low})
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile())
        return bd

    def test_strong_close_gets_5_pts(self):
        """close at 80% of bar range → +5."""
        # range = 10, close at 8 from low → ratio = 0.80 > 0.70
        bd = self._run(close=108.0, high=110.0, low=100.0)
        assert bd.close_strength_pts == 5

    def test_weak_close_gets_0_pts(self):
        """close at 50% of bar range → 0."""
        # range = 10, close at 5 from low → ratio = 0.50 < 0.70
        bd = self._run(close=105.0, high=110.0, low=100.0)
        assert bd.close_strength_pts == 0

    def test_exactly_at_threshold_gets_0_pts(self):
        """ratio = exactly 0.70 does NOT qualify (> 0.70 required)."""
        bd = self._run(close=107.0, high=110.0, low=100.0)
        assert bd.close_strength_pts == 0

    def test_zero_bar_range_skipped(self):
        """high == low → bar range is 0 → skip (CLOSE_STRENGTH_SKIP flag)."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 100.0, "high": 100.0, "low": 100.0})
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile())
        assert bd.close_strength_pts == 0
        assert any("CLOSE_STRENGTH_SKIP" in f for f in bd.flags)


# ------------------------------------------------------------------
# Factor 2: 連漲天數 (consec_up_pts)
# ------------------------------------------------------------------

class TestConsecUpScore:
    """Factor 2: close has risen for 3+ consecutive sessions → +5 pts."""

    def test_three_consec_up_gets_5_pts(self):
        """History where close rises for 3 sessions → +5."""
        # _make_history creates ascending closes (each +0.1), so last 3 are always rising.
        # Pass history[:-1] so engine's "sorted(history) + [ohlcv]" doesn't duplicate today.
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1]
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history[:-1], chip, _make_volume_profile())
        assert bd.consec_up_pts == 5

    def test_two_consec_up_gets_0_pts(self):
        """Only 2 consecutive up days → 0 pts."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        # Make index -3 (day 22) higher than day 23 → breaks streak at 3rd-to-last
        history[-3] = history[-3].model_copy(update={"close": history[-2].close + 5.0})
        ohlcv = history[-1]
        chip = _make_chip_report()
        # Pass history[:-1]: engine sees days 0..23 + ohlcv(day 24)
        # Streak: day24 > day23 ✓, day23 < day22(=day23.close+5) → break → count=1 < 3 → 0
        _, bd = engine.score_with_breakdown(ohlcv, history[:-1], chip, _make_volume_profile())
        assert bd.consec_up_pts == 0

    def test_insufficient_history_gets_0_pts(self):
        """Fewer than 3 sessions in history+today → can't confirm streak → 0 pts."""
        engine = TripleConfirmationEngine()
        history = _make_history(2)
        ohlcv = history[-1]
        chip = _make_chip_report()
        # history[:-1] = 1 bar; engine builds 1+1=2 bars → len < 3 → returns 0
        _, bd = engine.score_with_breakdown(ohlcv, history[:-1], chip, _make_volume_profile())
        assert bd.consec_up_pts == 0


# ------------------------------------------------------------------
# Factor 3: 均線多頭排列 (ma_alignment_pts)
# ------------------------------------------------------------------

class TestMAAlignmentScore:
    """Factor 3: MA5 > MA10 > MA20 → +5 pts."""

    def test_bull_ma_alignment_gets_5_pts(self):
        """Ascending history → MA5 > MA10 > MA20 → +5."""
        # _make_history produces ascending closes → MA5 > MA10 > MA20
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1]
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile())
        assert bd.ma_alignment_pts == 5

    def test_descending_history_gets_0_pts(self):
        """Descending closes → MA5 < MA10 < MA20 → 0 pts."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=200.0)
        # Reverse so closes descend (higher index = lower close)
        for i, bar in enumerate(history):
            history[i] = bar.model_copy(update={"close": 200.0 - i * 0.5})
        ohlcv = history[-1]
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile())
        assert bd.ma_alignment_pts == 0

    def test_insufficient_history_gets_0_pts_and_flag(self):
        """Fewer than 20 sessions → 0 pts + flag."""
        engine = TripleConfirmationEngine()
        history = _make_history(15)
        ohlcv = history[-1]
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile())
        assert bd.ma_alignment_pts == 0
        assert any("MA_ALIGNMENT" in f for f in bd.flags)


# ------------------------------------------------------------------
# Factor 4: 三大法人同向 (twse_all_inst_pts) — free-tier only
# ------------------------------------------------------------------

def _make_twse_proxy_full(
    *,
    foreign_net_buy: int = 0,
    trust_net_buy: int = 0,
    dealer_net_buy: int = 0,
    margin_balance_change: int = 0,
    foreign_consecutive_buy_days: int = 0,
    short_balance_increased: bool = False,
    short_margin_ratio: float = 0.0,
    is_available: bool = True,
) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="9999",
        trade_date=date(2025, 1, 31),
        foreign_net_buy=foreign_net_buy,
        trust_net_buy=trust_net_buy,
        dealer_net_buy=dealer_net_buy,
        margin_balance_change=margin_balance_change,
        foreign_consecutive_buy_days=foreign_consecutive_buy_days,
        short_balance_increased=short_balance_increased,
        short_margin_ratio=short_margin_ratio,
        is_available=is_available,
    )


class TestAllInstScore:
    """Factor 4: all three institutions net buy > 0 → +5 pts (free-tier only)."""

    def test_all_three_positive_gets_5_pts(self):
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=1_000, trust_net_buy=500, dealer_net_buy=200)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_all_inst_pts == 5

    def test_only_foreign_positive_gets_0_pts(self):
        """Trust or dealer is 0 → not 三大法人同向."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=1_000, trust_net_buy=0, dealer_net_buy=200)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_all_inst_pts == 0

    def test_one_negative_gets_0_pts(self):
        """Any institution selling → not all-in → 0 pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=1_000, trust_net_buy=-500, dealer_net_buy=200)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_all_inst_pts == 0


# ------------------------------------------------------------------
# Factor 5: 外資連買天數 (twse_foreign_consec_pts) — free-tier only
# ------------------------------------------------------------------

class TestForeignConsecScore:
    """Factor 5: foreign_consecutive_buy_days >= 3 → +5 pts (free-tier only)."""

    def test_three_or_more_gets_5_pts(self):
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=1_000, foreign_consecutive_buy_days=3)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_consec_pts == 5

    def test_five_days_also_gets_5_pts(self):
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=1_000, foreign_consecutive_buy_days=5)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_consec_pts == 5

    def test_two_days_gets_0_pts(self):
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=1_000, foreign_consecutive_buy_days=2)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_consec_pts == 0

    def test_zero_days_gets_0_pts(self):
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(foreign_net_buy=0, foreign_consecutive_buy_days=0)
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.twse_foreign_consec_pts == 0


# ------------------------------------------------------------------
# Factor 6: RS vs 大盤 (rs_pts)
# ------------------------------------------------------------------

def _make_taiex_history(n: int, base_close: float = 20_000.0) -> list[DailyOHLCV]:
    """Generate n days of TAIEX OHLCV (flat by default)."""
    result = []
    d = date(2025, 1, 2)
    for i in range(n):
        close = base_close + i * 1.0  # slight upward drift (0.005% per day)
        result.append(
            DailyOHLCV(
                ticker="TAIEX",
                trade_date=d + timedelta(days=i),
                open=close,
                high=close + 50,
                low=close - 50,
                close=close,
                volume=1_000_000_000,
            )
        )
    return result


class TestRSScore:
    """Factor 6: stock 5d return > TAIEX 5d return × 1.2 → +5 pts."""

    def test_outperforming_stock_gets_5_pts(self):
        """Stock rises 10% vs TAIEX 5% → 10% > 5% × 1.2 = 6% → +5."""
        engine = TripleConfirmationEngine()
        # Stock: starts at 100, ends at 110 (+10% in 5 days)
        history = _make_history(25, base_close=100.0)
        # Make last 5 bars: 100 → 105 → 106 → 108 → 110
        for i in range(5):
            history[-(5-i)] = history[-(5-i)].model_copy(update={"close": 100.0 + i * 2.5})
        ohlcv = history[-1].model_copy(update={"close": 110.0})

        # TAIEX: starts at 20_000, ends at 21_000 (+5%)
        taiex = _make_taiex_history(25, base_close=20_000.0)
        for i in range(5):
            taiex[-(5-i)] = taiex[-(5-i)].model_copy(update={"close": 20_000.0 + i * 250.0})
        taiex[-1] = taiex[-1].model_copy(update={"close": 21_000.0})

        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile(), taiex_history=taiex)
        assert bd.rs_pts == 5

    def test_underperforming_stock_gets_0_pts(self):
        """Stock rises 3% vs TAIEX 5% → 3% < 5% × 1.2 = 6% → 0 pts."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0)
        ohlcv = history[-1].model_copy(update={"close": 103.0})

        taiex = _make_taiex_history(25, base_close=20_000.0)
        taiex[-1] = taiex[-1].model_copy(update={"close": 21_000.0})

        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile(), taiex_history=taiex)
        assert bd.rs_pts == 0

    def test_no_taiex_history_gets_0_pts(self):
        """No TAIEX data provided → RS score skipped → 0 pts."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1]
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(ohlcv, history, chip, _make_volume_profile())
        assert bd.rs_pts == 0

    def test_insufficient_taiex_history_gets_0_pts(self):
        """Fewer than 5 TAIEX bars → RS score skipped → 0 pts + flag."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        taiex = _make_taiex_history(3)
        chip = _make_chip_report()
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), taiex_history=taiex)
        assert bd.rs_pts == 0
        assert any("INSUFFICIENT_HISTORY_RS" in f for f in bd.flags)


# ------------------------------------------------------------------
# Factor 7: 融券餘額暴增 + 券資比 (short_spike_deduction) — free-tier only
# ------------------------------------------------------------------

class TestShortSpikeDeduction:
    """Factor 7: 融券暴增 AND 券資比 > 15% → -10 pts risk deduction."""

    def test_spike_and_high_ratio_gets_deduction(self):
        """short_balance_increased=True AND short_margin_ratio=0.20 → -10 pts."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(
            foreign_net_buy=1_000,
            short_balance_increased=True,
            short_margin_ratio=0.20,
        )
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.short_spike_deduction == 10
        assert any("SHORT_SPIKE" in f for f in bd.flags)

    def test_spike_but_low_ratio_no_deduction(self):
        """short_balance_increased=True but ratio=0.10 (<= 0.15) → no deduction."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(
            foreign_net_buy=1_000,
            short_balance_increased=True,
            short_margin_ratio=0.10,
        )
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.short_spike_deduction == 0

    def test_high_ratio_but_no_spike_no_deduction(self):
        """short_margin_ratio=0.20 but short_balance_increased=False → no deduction."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()
        proxy = _make_twse_proxy_full(
            foreign_net_buy=1_000,
            short_balance_increased=False,
            short_margin_ratio=0.20,
        )
        _, bd = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy)
        assert bd.short_spike_deduction == 0

    def test_deduction_reduces_total_score(self):
        """The -10 deduction is reflected in the total score."""
        engine = TripleConfirmationEngine(free_tier_mode=True)
        history = _make_history(25)
        chip = _make_empty_chip_report()

        proxy_clean = _make_twse_proxy_full(
            foreign_net_buy=1_000,
            short_balance_increased=False,
            short_margin_ratio=0.0,
        )
        proxy_spike = _make_twse_proxy_full(
            foreign_net_buy=1_000,
            short_balance_increased=True,
            short_margin_ratio=0.20,
        )

        _, bd_clean = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy_clean)
        _, bd_spike = engine.score_with_breakdown(history[-1], history, chip, _make_volume_profile(), twse_proxy=proxy_spike)

        assert bd_spike.total == bd_clean.total - 10


# ------------------------------------------------------------------
# Round 2 tests: Change A — volume surge direction check
# ------------------------------------------------------------------

def _make_history_with_down_last(n: int = 25, base_vol: int = 10_000) -> list[DailyOHLCV]:
    """History where the last entry has a down-close (for distribution day tests)."""
    result = []
    d = date(2025, 1, 2)
    for i in range(n):
        close = 100.0 + i * 0.1
        result.append(
            DailyOHLCV(
                ticker="9999",
                trade_date=d + timedelta(days=i),
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.5,
                close=close,
                volume=base_vol,
            )
        )
    # Replace last entry: close is lower than second-to-last (distribution day)
    prev_close = result[-2].close
    result[-1] = result[-1].model_copy(update={"close": prev_close - 0.5})
    return result


class TestVolumeSurgeDirection:
    """Change A: volume surge on distribution day (close < prev_close) gets 0."""

    def test_accumulation_day_gets_20_pts(self):
        """High volume + close >= prev_close → +20."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_vol=10_000)
        # close is ascending so last close > prev close
        ohlcv = history[-1].model_copy(update={"volume": 20_000})
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), _make_volume_profile())
        assert bd.volume_surge_pts == 20
        assert "VOLUME_DISTRIBUTION" not in bd.flags

    def test_distribution_day_gets_0_pts_and_flag(self):
        """High volume + close < prev_close → 0 + VOLUME_DISTRIBUTION flag."""
        engine = TripleConfirmationEngine()
        history = _make_history_with_down_last(25, base_vol=10_000)
        ohlcv = history[-1].model_copy(update={"volume": 20_000})
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), _make_volume_profile())
        assert bd.volume_surge_pts == 0
        assert "VOLUME_DISTRIBUTION" in bd.flags

    def test_flat_day_is_not_distribution(self):
        """High volume + close == prev_close → +20 (flat is not distribution)."""
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_vol=10_000)
        prev_close = history[-2].close
        ohlcv = history[-1].model_copy(update={"close": prev_close, "volume": 20_000})
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), _make_volume_profile())
        assert bd.volume_surge_pts == 20
        assert "VOLUME_DISTRIBUTION" not in bd.flags

    def test_no_surge_skips_direction_check(self):
        """Volume below 1.5x threshold → 0 pts, no flag regardless of direction."""
        engine = TripleConfirmationEngine()
        history = _make_history_with_down_last(25, base_vol=10_000)
        ohlcv = history[-1].model_copy(update={"volume": 11_000})  # 1.1x < 1.5x
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), _make_volume_profile())
        assert bd.volume_surge_pts == 0
        assert "VOLUME_DISTRIBUTION" not in bd.flags

    def test_no_prev_history_gives_benefit_of_doubt(self):
        """If no prior day exists in history, surge gets +20 (benefit of doubt).

        Constructed by making all 25 history entries share the same trade_date as
        ohlcv, so prev_day filter (trade_date < ohlcv.trade_date) returns empty.
        vol20ma still computes because we have 25 entries.
        """
        engine = TripleConfirmationEngine()
        same_date = date(2025, 1, 31)
        history = [
            DailyOHLCV(
                ticker="9999", trade_date=same_date,
                open=99.5, high=101.0, low=99.0, close=100.0, volume=10_000,
            )
            for _ in range(25)
        ]
        ohlcv = history[-1].model_copy(update={"volume": 20_000})
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), _make_volume_profile())
        assert bd.volume_surge_pts == 20


# ------------------------------------------------------------------
# Round 2 tests: Change B — volume accumulation trend
# ------------------------------------------------------------------

def _make_history_with_volume_trend(
    n: int = 25,
    base_vol: int = 10_000,
    increasing: bool = True,
) -> list[DailyOHLCV]:
    """History where last 3 sessions (before today) have increasing or flat volume."""
    result = _make_history(n, base_vol=base_vol)
    if increasing:
        # Set sessions [-4], [-3], [-2] to strictly increasing volume
        result[-4] = result[-4].model_copy(update={"volume": 8_000})
        result[-3] = result[-3].model_copy(update={"volume": 9_000})
        result[-2] = result[-2].model_copy(update={"volume": 10_000})
    else:
        # Flat volume — no trend
        result[-4] = result[-4].model_copy(update={"volume": 10_000})
        result[-3] = result[-3].model_copy(update={"volume": 10_000})
        result[-2] = result[-2].model_copy(update={"volume": 10_000})
    return result


class TestVolumeTrend:
    """Change B: 3 consecutive increasing prior sessions → +5."""

    def test_three_increasing_sessions_gets_5_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history_with_volume_trend(25, increasing=True)
        _, bd = engine.score_with_breakdown(
            history[-1], history, _make_chip_report(), _make_volume_profile()
        )
        assert bd.volume_trend_pts == 5

    def test_flat_volume_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history_with_volume_trend(25, increasing=False)
        _, bd = engine.score_with_breakdown(
            history[-1], history, _make_chip_report(), _make_volume_profile()
        )
        assert bd.volume_trend_pts == 0

    def test_insufficient_history_gets_0_pts(self):
        """Fewer than 4 sessions → can't compute trend."""
        engine = TripleConfirmationEngine()
        history = _make_history(3)
        _, bd = engine.score_with_breakdown(
            history[-1], history, _make_chip_report(), _make_volume_profile()
        )
        assert bd.volume_trend_pts == 0

    def test_trend_uses_prior_sessions_not_today(self):
        """Today's volume is NOT checked; only sessions [-4], [-3], [-2] matter."""
        engine = TripleConfirmationEngine()
        history = _make_history_with_volume_trend(25, increasing=True)
        # Give today massive volume — should not affect trend score
        ohlcv = history[-1].model_copy(update={"volume": 1_000_000})
        _, bd = engine.score_with_breakdown(
            ohlcv, history, _make_chip_report(), _make_volume_profile()
        )
        assert bd.volume_trend_pts == 5

    def test_decreasing_volume_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_vol=10_000)
        history[-4] = history[-4].model_copy(update={"volume": 12_000})
        history[-3] = history[-3].model_copy(update={"volume": 11_000})
        history[-2] = history[-2].model_copy(update={"volume": 10_000})
        _, bd = engine.score_with_breakdown(
            history[-1], history, _make_chip_report(), _make_volume_profile()
        )
        assert bd.volume_trend_pts == 0


# ------------------------------------------------------------------
# Round 2 tests: Change C — 60-day high breakout
# ------------------------------------------------------------------

def _make_volume_profile_with_60d(
    twenty_day_high: float = 115.0,
    sixty_day_high: float = 120.0,
    sixty_day_sessions: int = 60,
) -> VolumeProfile:
    return VolumeProfile(
        ticker="9999",
        period_end=date(2025, 1, 31),
        poc_proxy=twenty_day_high,
        twenty_day_high=twenty_day_high,
        twenty_day_sessions=20,
        sixty_day_high=sixty_day_high,
        sixty_day_sessions=sixty_day_sessions,
    )


class TestSixtyDayHighScore:
    """Change C: close within 1% of 60d high → +10."""

    def test_close_above_60d_high_gets_10_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 121.0})
        vp = _make_volume_profile_with_60d(twenty_day_high=115.0, sixty_day_high=120.0)
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), vp)
        assert bd.sixty_day_high_pts == 10

    def test_close_within_1pct_of_60d_high_gets_10_pts(self):
        """close > sixty_day_high * 0.99 → fires."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 119.5})  # 119.5 > 120 * 0.99 = 118.8
        vp = _make_volume_profile_with_60d(twenty_day_high=115.0, sixty_day_high=120.0)
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), vp)
        assert bd.sixty_day_high_pts == 10

    def test_close_below_60d_high_gets_0_pts(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 110.0})
        vp = _make_volume_profile_with_60d(twenty_day_high=115.0, sixty_day_high=120.0)
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), vp)
        assert bd.sixty_day_high_pts == 0

    def test_insufficient_sessions_gets_0_and_flag(self):
        """fewer than 40 sessions → 0 pts + INSUFFICIENT_HISTORY_60D_HIGH flag."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 121.0})
        vp = _make_volume_profile_with_60d(
            twenty_day_high=115.0, sixty_day_high=120.0, sixty_day_sessions=35
        )
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), vp)
        assert bd.sixty_day_high_pts == 0
        assert "INSUFFICIENT_HISTORY_60D_HIGH" in bd.flags

    def test_genuine_breakout_fires_both_20d_and_60d(self):
        """A real breakout (close above both 20d and 60d high) scores both (+30 total space pts)."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 130.0})
        vp = _make_volume_profile_with_60d(
            twenty_day_high=115.0, sixty_day_high=120.0, sixty_day_sessions=60
        )
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), vp)
        assert bd.space_pts == 20
        assert bd.sixty_day_high_pts == 10

    def test_60d_same_as_20d_only_20d_fires(self):
        """When 60d_high == 20d_high (short history), only 20d fires if close clears it."""
        engine = TripleConfirmationEngine()
        history = _make_history(25)
        ohlcv = history[-1].model_copy(update={"close": 116.0})
        # sixty_day_sessions < 40 → flag, no 60d pts
        vp = _make_volume_profile_with_60d(
            twenty_day_high=115.0, sixty_day_high=115.0, sixty_day_sessions=20
        )
        _, bd = engine.score_with_breakdown(ohlcv, history, _make_chip_report(), vp)
        assert bd.space_pts == 20
        assert bd.sixty_day_high_pts == 0
        assert "INSUFFICIENT_HISTORY_60D_HIGH" in bd.flags


# ------------------------------------------------------------------
# Round 2 tests: Change D — high52w_pct hint
# ------------------------------------------------------------------

class TestHigh52wPctHint:
    """Change D: high52w_pct computed from available history."""

    def test_close_at_period_high_gives_zero_pct(self):
        engine = TripleConfirmationEngine()
        # All candles have high=close+1.0; ohlcv.close = highest close in history
        history = _make_history(25, base_close=100.0)
        ohlcv = history[-1]  # highest close
        _, _, hints = engine.score_full(
            ohlcv, history, _make_chip_report(), _make_volume_profile()
        )
        assert hints is not None
        assert hints.high52w_pct is not None
        # close is near period high → pct should be <= 0 and close to 0
        assert hints.high52w_pct <= 0.0

    def test_close_below_period_high_gives_negative_pct(self):
        engine = TripleConfirmationEngine()
        history = _make_history(25, base_close=100.0)
        # Set a clearly higher high in earlier candles
        history[10] = history[10].model_copy(update={"high": 200.0})
        ohlcv = history[-1].model_copy(update={"close": 102.0})
        _, _, hints = engine.score_full(
            ohlcv, history, _make_chip_report(), _make_volume_profile()
        )
        assert hints is not None
        assert hints.high52w_pct is not None
        assert hints.high52w_pct < 0.0  # well below the 200 spike

    def test_no_history_gives_none(self):
        engine = TripleConfirmationEngine()
        history = []
        ohlcv = DailyOHLCV(
            ticker="9999", trade_date=date(2025, 1, 31),
            open=100.0, high=101.0, low=99.0, close=100.0, volume=10_000
        )
        _, _, hints = engine.score_full(
            ohlcv, history, _make_chip_report(), _make_volume_profile()
        )
        assert hints is not None
        assert hints.high52w_pct is None

    def test_hint_does_not_affect_score(self):
        """high52w_pct is informational only — total score must not change."""
        engine = TripleConfirmationEngine()
        history_normal = _make_history(25, base_close=100.0)
        history_spike = list(history_normal)
        history_spike[5] = history_spike[5].model_copy(update={"high": 500.0})
        ohlcv = history_normal[-1]

        _, bd_normal, _ = engine.score_full(ohlcv, history_normal, _make_chip_report(), _make_volume_profile())
        _, bd_spike, _ = engine.score_full(ohlcv, history_spike, _make_chip_report(), _make_volume_profile())
        assert bd_normal.total == bd_spike.total
