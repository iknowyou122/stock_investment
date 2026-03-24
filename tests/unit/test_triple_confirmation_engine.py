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
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.models import (
    BrokerWithLabel,
    ChipReport,
    DailyOHLCV,
    VolumeProfile,
)
from taiwan_stock_agent.domain.triple_confirmation_engine import TripleConfirmationEngine


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
