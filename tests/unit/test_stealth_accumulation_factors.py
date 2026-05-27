"""Tests for Phase 4.32 stealth accumulation factors."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.models import TWSEChipProxy
from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.triple_confirmation_engine import TripleConfirmationEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proxy(**kwargs) -> TWSEChipProxy:
    defaults = dict(
        ticker="TEST",
        trade_date=date(2026, 1, 2),
        is_available=True,
    )
    defaults.update(kwargs)
    return TWSEChipProxy(**defaults)


def _ohlcv(close: float, volume: int = 1_000_000, trade_date: date | None = None) -> DailyOHLCV:
    d = trade_date or date(2026, 1, 2)
    return DailyOHLCV(
        ticker="TEST",
        trade_date=d,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=volume,
    )


def _history(n: int = 20, base_close: float = 100.0, flat: bool = True) -> list[DailyOHLCV]:
    """Generate n days of history. flat=True: price stays near base_close."""
    bars = []
    start = date(2026, 1, 2) - timedelta(days=n + 5)
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        close = base_close if flat else base_close * (1 + 0.005 * i)
        bars.append(_ohlcv(close, trade_date=d))
    return bars


TCE = TripleConfirmationEngine


# ---------------------------------------------------------------------------
# Factor 1: obv_stealth_pts
# ---------------------------------------------------------------------------

class TestOBVStealth:
    def test_fires_when_obv_rising_and_price_flat(self):
        # Rising volume on up days (OBV goes up) but price stays ~100
        history = []
        base = date(2025, 12, 1)
        prev_close = 100.0
        for i in range(10):
            d = base + timedelta(days=i)
            close = 100.0 + (0.1 if i % 2 == 0 else -0.05)  # slight fluctuation
            history.append(_ohlcv(close, volume=2_000_000 if i % 2 == 0 else 500_000, trade_date=d))
            prev_close = close
        today = _ohlcv(100.1)
        pts, flag = TCE._obv_stealth_score(today, history)
        # OBV slope positive (more volume on up days) + price barely moved
        assert pts == 3
        assert flag == "OBV_STEALTH"

    def test_no_signal_when_price_surging(self):
        history = []
        base = date(2025, 12, 1)
        for i in range(10):
            history.append(_ohlcv(100.0 + i * 0.5, volume=2_000_000, trade_date=base + timedelta(days=i)))
        today = _ohlcv(105.0)  # 5% up from base → not flat
        pts, flag = TCE._obv_stealth_score(today, history)
        assert pts == 0
        assert flag is None

    def test_no_signal_with_insufficient_history(self):
        pts, flag = TCE._obv_stealth_score(_ohlcv(100.0), [_ohlcv(100.0)])
        assert pts == 0

    def test_obv_declining_gives_zero(self):
        # More volume on down days → OBV falling
        history = []
        base = date(2025, 12, 1)
        for i in range(10):
            close = 100.0 - i * 0.1
            history.append(_ohlcv(close, volume=2_000_000, trade_date=base + timedelta(days=i)))
        today = _ohlcv(99.0)
        pts, flag = TCE._obv_stealth_score(today, history)
        assert pts == 0


# ---------------------------------------------------------------------------
# Factor 2: margin_persist_decline_pts
# ---------------------------------------------------------------------------

class TestMarginPersistDecline:
    def test_streak_5_gives_4pts(self):
        proxy = _proxy(margin_decline_streak=5)
        assert TCE._margin_persist_decline_score(proxy) == 4

    def test_streak_3_gives_2pts(self):
        proxy = _proxy(margin_decline_streak=3)
        assert TCE._margin_persist_decline_score(proxy) == 2

    def test_streak_2_gives_0pts(self):
        proxy = _proxy(margin_decline_streak=2)
        assert TCE._margin_persist_decline_score(proxy) == 0

    def test_streak_0_gives_0pts(self):
        proxy = _proxy(margin_decline_streak=0)
        assert TCE._margin_persist_decline_score(proxy) == 0

    def test_streak_7_gives_4pts(self):
        proxy = _proxy(margin_decline_streak=7)
        assert TCE._margin_persist_decline_score(proxy) == 4


# ---------------------------------------------------------------------------
# Factor 3: holder_count_declining_pts
# ---------------------------------------------------------------------------

class TestHolderCountDeclining:
    def test_two_weeks_decline_gives_5pts(self):
        proxy = _proxy(holder_count_chg_weekly=-200, holder_count_decline_weeks=2)
        pts, flag = TCE._holder_count_declining_score(proxy)
        assert pts == 5
        assert "HOLDER_SHRINK" in flag
        assert "2w" in flag

    def test_one_week_decline_gives_3pts(self):
        proxy = _proxy(holder_count_chg_weekly=-100, holder_count_decline_weeks=1)
        pts, flag = TCE._holder_count_declining_score(proxy)
        assert pts == 3
        assert "1w" in flag

    def test_no_finmind_key_gives_zero(self):
        proxy = _proxy(holder_count_chg_weekly=None, holder_count_decline_weeks=0)
        pts, flag = TCE._holder_count_declining_score(proxy)
        assert pts == 0
        assert flag is None

    def test_holder_count_increasing_gives_zero(self):
        proxy = _proxy(holder_count_chg_weekly=500, holder_count_decline_weeks=0)
        pts, flag = TCE._holder_count_declining_score(proxy)
        assert pts == 0


# ---------------------------------------------------------------------------
# Factor 4: chip_concentration_accel_pts
# ---------------------------------------------------------------------------

class TestChipConcentrationAccel:
    def test_acceleration_with_super_large_continuous(self):
        """Phase 4.44 continuous: this_week=1.0%, peak=6.0 at 1.5%, linear floor 3.0 at 0.5%.
        1.0% → 3.0 + (1.0-0.5)/1.0*(6-3) = 4.5."""
        proxy = _proxy(
            large_holder_chg_pct=1.0,
            large_holder_2w_trend=1.3,
            super_large_holder_chg_pct=0.5,
        )
        pts, flag = TCE._chip_concentration_accel_score(proxy)
        assert pts == pytest.approx(4.5, abs=0.05)
        assert "CHIP_ACCEL_PRIME" in flag

    def test_acceleration_without_super_large_gives_3pts(self):
        proxy = _proxy(
            large_holder_chg_pct=0.8,
            large_holder_2w_trend=1.0,    # last_week=0.2
            super_large_holder_chg_pct=0.1,  # too small
        )
        pts, flag = TCE._chip_concentration_accel_score(proxy)
        assert pts == 3
        assert "CHIP_ACCEL" in flag

    def test_deceleration_gives_zero(self):
        # this_week=0.2%, last_week=0.8% → deceleration
        proxy = _proxy(
            large_holder_chg_pct=0.2,
            large_holder_2w_trend=1.0,    # last_week=0.8
        )
        pts, flag = TCE._chip_concentration_accel_score(proxy)
        assert pts == 0

    def test_below_threshold_gives_zero(self):
        proxy = _proxy(
            large_holder_chg_pct=0.3,    # < 0.5% threshold
            large_holder_2w_trend=0.4,
        )
        pts, flag = TCE._chip_concentration_accel_score(proxy)
        assert pts == 0

    def test_missing_data_gives_zero(self):
        proxy = _proxy(large_holder_chg_pct=None, large_holder_2w_trend=None)
        pts, flag = TCE._chip_concentration_accel_score(proxy)
        assert pts == 0


# ---------------------------------------------------------------------------
# Factor 5: short_squeeze_setup_pts
# ---------------------------------------------------------------------------

class TestShortSqueezeSetup:
    def test_high_ratio_high_cover_gives_5pts(self):
        proxy = _proxy(short_margin_ratio=0.45, short_cover_rate=0.18)
        pts, flag = TCE._short_squeeze_setup_score(proxy)
        assert pts == 5
        assert "SHORT_SQUEEZE_SETUP" in flag

    def test_medium_ratio_medium_cover_continuous(self):
        """Phase 4.44: 0.25→3.0, 0.40→5.0. SMR=0.30 → 3.0 + (0.30-0.25)/0.15*2 = 3.67."""
        proxy = _proxy(short_margin_ratio=0.30, short_cover_rate=0.10)
        pts, flag = TCE._short_squeeze_setup_score(proxy)
        assert pts == pytest.approx(3.67, abs=0.05)
        assert "SHORT_SQUEEZE_SETUP" in flag

    def test_low_ratio_gives_zero(self):
        proxy = _proxy(short_margin_ratio=0.15, short_cover_rate=0.15)
        pts, flag = TCE._short_squeeze_setup_score(proxy)
        assert pts == 0

    def test_high_ratio_but_no_covering_gives_zero(self):
        proxy = _proxy(short_margin_ratio=0.35, short_cover_rate=0.02)
        pts, flag = TCE._short_squeeze_setup_score(proxy)
        assert pts == 0

    def test_zero_smr_gives_zero(self):
        proxy = _proxy(short_margin_ratio=0.0, short_cover_rate=0.20)
        pts, flag = TCE._short_squeeze_setup_score(proxy)
        assert pts == 0


# ---------------------------------------------------------------------------
# Factor 6: stealth_accum_composite_pts
# ---------------------------------------------------------------------------

class TestStealthAccumComposite:
    def _make_bd_with_signals(
        self,
        obv_stealth: int = 0,
        volume_dryup: int = 0,
        holder_declining: int = 0,
        chip_accel: int = 0,
    ):
        from taiwan_stock_agent.domain.triple_confirmation_engine import _ScoreBreakdown
        bd = _ScoreBreakdown()
        bd.obv_stealth_pts = obv_stealth
        bd.volume_dryup_pts = volume_dryup
        bd.holder_count_declining_pts = holder_declining
        bd.chip_concentration_accel_pts = chip_accel
        return bd

    def test_5_of_6_gives_prime(self):
        bd = self._make_bd_with_signals(obv_stealth=3, volume_dryup=8, holder_declining=3, chip_accel=3)
        proxy = _proxy(margin_decline_streak=4)
        # history with flat price
        history = _history(15, base_close=100.0, flat=True)
        today = _ohlcv(100.2)
        pts, flag = TCE._stealth_accum_composite_score(bd, today, history, proxy)
        assert pts == 10
        assert "STEALTH_ACCUM_PRIME" in flag

    def test_4_of_6_gives_accum(self):
        # [1] obv=1, [4] chip_accel=1, [5] dryup=1, [6] flat price=1 → 4/6
        # margin_streak=0 (−), holder=0 (−)
        bd = self._make_bd_with_signals(obv_stealth=3, volume_dryup=8, holder_declining=0, chip_accel=3)
        proxy = _proxy(margin_decline_streak=0)
        history = _history(15, base_close=100.0, flat=True)
        today = _ohlcv(100.2)
        pts, flag = TCE._stealth_accum_composite_score(bd, today, history, proxy)
        assert pts == 6
        assert "STEALTH_ACCUM" in flag

    def test_3_of_6_gives_zero(self):
        bd = self._make_bd_with_signals(obv_stealth=3, volume_dryup=8)
        proxy = _proxy(margin_decline_streak=0)
        history = _history(15, base_close=100.0, flat=False)  # price not flat
        today = _ohlcv(105.0)  # 5%+ up → not flat
        pts, flag = TCE._stealth_accum_composite_score(bd, today, history, proxy)
        assert pts == 0

    def test_no_proxy_still_works(self):
        bd = self._make_bd_with_signals(obv_stealth=3, volume_dryup=8)
        history = _history(15, base_close=100.0, flat=True)
        today = _ohlcv(100.2)
        pts, flag = TCE._stealth_accum_composite_score(bd, today, history, None)
        assert isinstance(pts, (int, float))  # Phase 4.44: now float

    def test_price_surge_breaks_condition_6_and_drops_to_zero(self):
        # Only 3/6 conditions met: [5] dryup, [3] holder, [4] accel
        # obv=0, margin_streak=0, price surges > 3% → condition 6 fails
        # → 3/6 → 0 pts
        bd = self._make_bd_with_signals(obv_stealth=0, volume_dryup=8, holder_declining=3, chip_accel=3)
        proxy = _proxy(margin_decline_streak=0)
        history = _history(12, base_close=85.0, flat=False)
        today = _ohlcv(100.0)  # ~18% from base → condition 6 fails
        pts, flag = TCE._stealth_accum_composite_score(bd, today, history, proxy)
        assert pts == 0
        assert flag is None


# ---------------------------------------------------------------------------
# TWSEChipProxy model has new fields
# ---------------------------------------------------------------------------

class TestTWSEChipProxyNewFields:
    def test_default_values(self):
        proxy = _proxy()
        assert proxy.margin_decline_streak == 0
        assert proxy.holder_count_chg_weekly is None
        assert proxy.holder_count_decline_weeks == 0

    def test_can_set_fields(self):
        proxy = _proxy(
            margin_decline_streak=5,
            holder_count_chg_weekly=-300,
            holder_count_decline_weeks=2,
        )
        assert proxy.margin_decline_streak == 5
        assert proxy.holder_count_chg_weekly == -300
        assert proxy.holder_count_decline_weeks == 2
