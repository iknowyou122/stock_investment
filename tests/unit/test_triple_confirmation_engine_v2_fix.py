import pytest
from datetime import date, timedelta
from taiwan_stock_agent.domain.triple_confirmation_engine import TripleConfirmationEngine
from taiwan_stock_agent.domain.models import DailyOHLCV, ChipReport, VolumeProfile, BrokerWithLabel

def _make_ohlcv(close=100.0, volume=10000, trade_date=None):
    return DailyOHLCV(ticker="TEST", trade_date=trade_date or date(2025, 2, 1),
                    open=close, high=close+1, low=close-1, close=close, volume=volume)

def _make_history(n=20, base_close=100.0, base_vol=10000, flat=True):
    return [_make_ohlcv(base_close, base_vol, date(2025, 1, 1) + timedelta(days=i)) for i in range(n)]

def _make_volume_profile(twenty_day_high=110.0):
    return VolumeProfile(ticker="TEST", period_end=date(2025, 2, 1), poc_proxy=100.0,
                        twenty_day_high=twenty_day_high, sixty_day_high=120.0,
                        twenty_day_sessions=20, sixty_day_sessions=0,
                        one_twenty_day_sessions=120, fiftytwo_week_sessions=250)

def _make_chip_report():
    return ChipReport(ticker="TEST", report_date=date(2025, 2, 1), net_buyer_count_diff=0, active_branch_count=0,
                     concentration_top15=0.0, top_buyers=[], risk_flags=[])

class TestV23Core:
    def test_new_gate_pre_breakout(self):
        """New v2.3 gate: Price in 85-99% zone, BB tight, Liq pass, TAIEX not down."""
        eng = TripleConfirmationEngine()
        # G1: close 95 / high 100 = 0.95 (pass)
        # G2: flat hist -> BB tight (pass)
        # G3: vol 1M -> liq pass
        # G4: no taiex history provided -> regime neutral (pass)
        hist = _make_history(30, base_vol=1_000_000)
        ohlcv = _make_ohlcv(close=95.0, volume=1_000_000)
        vp = _make_volume_profile(twenty_day_high=100.0)
        chip = _make_chip_report()
        
        signal, bd = eng.score_with_breakdown(ohlcv, hist, chip, vp)
        assert "NO_SETUP" not in signal.data_quality_flags
        assert any("GATE_PASS:G1_ZONE" in f for f in bd.flags)

    def test_gate_fails_already_broke_out(self):
        eng = TripleConfirmationEngine()
        hist = _make_history(30, base_vol=1_000_000)
        # G1: close 100 / high 100 = 1.0 (fail)
        ohlcv = _make_ohlcv(close=100.0, volume=1_000_000)
        vp = _make_volume_profile(twenty_day_high=100.0)
        chip = _make_chip_report()

        signal, bd = eng.score_with_breakdown(ohlcv, hist, chip, vp)
        assert "NO_SETUP" in signal.data_quality_flags
        assert any("GATE_FAIL:G1_ALREADY_BROKE_OUT" in f for f in bd.flags)

    def test_pillar3_proximity_at_92_percent(self):
        """Proximity score: 92-99% range → 12 pts."""
        eng = TripleConfirmationEngine()
        hist = _make_history(30, base_vol=1_000_000)
        # close 92 / high 100 = 0.92 (92% → 12 pts)
        ohlcv = _make_ohlcv(close=92.0, volume=1_000_000)
        vp = _make_volume_profile(twenty_day_high=100.0)
        chip = _make_chip_report()

        signal, bd = eng.score_with_breakdown(ohlcv, hist, chip, vp)
        assert bd.proximity_pts == 12

    def test_pillar3_proximity_at_88_percent(self):
        """Proximity score: 88-92% range → 6 pts."""
        eng = TripleConfirmationEngine()
        hist = _make_history(30, base_vol=1_000_000)
        # close 88 / high 100 = 0.88 (88% → 6 pts)
        ohlcv = _make_ohlcv(close=88.0, volume=1_000_000)
        vp = _make_volume_profile(twenty_day_high=100.0)
        chip = _make_chip_report()

        signal, bd = eng.score_with_breakdown(ohlcv, hist, chip, vp)
        assert bd.proximity_pts == 6

    def test_pillar3_bb_compression_tight(self):
        """BB compression: BB width < 8% → 10 pts."""
        eng = TripleConfirmationEngine()
        # Create flat history (BB width ~ 2%)
        hist = _make_history(25, base_close=100.0, base_vol=1_000_000, flat=True)
        ohlcv = _make_ohlcv(close=100.0, volume=1_000_000)
        vp = _make_volume_profile(twenty_day_high=102.0)
        chip = _make_chip_report()

        signal, bd = eng.score_with_breakdown(ohlcv, hist, chip, vp)
        assert bd.bb_compression_pts == 10

    def test_pillar3_ma_convergence(self):
        """MA5/MA10/MA20 convergence within 2% → 8 pts."""
        eng = TripleConfirmationEngine()
        # All MAs near same price
        hist = _make_history(25, base_close=100.0, base_vol=1_000_000, flat=True)
        ohlcv = _make_ohlcv(close=100.0, volume=1_000_000)
        vp = _make_volume_profile(twenty_day_high=102.0)
        chip = _make_chip_report()

        signal, bd = eng.score_with_breakdown(ohlcv, hist, chip, vp)
        assert bd.ma_convergence_pts == 8
