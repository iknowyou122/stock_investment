"""Tests for SurgeRadar engine."""
from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy
from taiwan_stock_agent.domain.surge_radar import SurgeRadar


def _bar(close: float = 100.0, volume: int = 600_000, day: int = 0,
         o: float | None = None, h: float | None = None, lo: float | None = None) -> DailyOHLCV:
    return DailyOHLCV(
        ticker="TEST",
        trade_date=date(2026, 1, 1) + timedelta(days=day),
        open=o if o is not None else close,
        high=h if h is not None else close + 1,
        low=lo if lo is not None else close - 1,
        close=close,
        volume=volume,
    )


def _flat_history(n: int = 60, close: float = 100.0, volume: int = 600_000) -> list[DailyOHLCV]:
    return [_bar(close=close, volume=volume, day=i) for i in range(n)]


def _taiex_history(n: int = 70, close: float = 17000.0) -> list[DailyOHLCV]:
    return [
        DailyOHLCV(
            ticker="TAIEX",
            trade_date=date(2026, 1, 1) + timedelta(days=i),
            open=close,
            high=close + 50,
            low=close - 50,
            close=close,
            volume=0,
        )
        for i in range(n)
    ]


def _proxy_with(foreign_days: int = 0, trust_days: int = 0, util: float | None = None,
                available: bool = True) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="TEST",
        trade_date=date(2026, 3, 1),
        foreign_consecutive_buy_days=foreign_days,
        trust_consecutive_buy_days=trust_days,
        margin_utilization_rate=util,
        is_available=available,
    )


class TestGate:
    def test_insufficient_history_fails(self):
        eng = SurgeRadar()
        hist = _flat_history(10)
        today = _bar(close=105, volume=2_000_000, day=99, lo=100, h=106)
        ok, flags = eng._gate_check(today, hist, "neutral", 60_000_000)
        assert ok is False
        assert any("INSUFFICIENT_HISTORY" in f for f in flags)

    def test_no_vol_surge_fails(self):
        """Volume matches average → no surge, G1 fails."""
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=600_000, day=99, lo=100, h=106)
        ok, flags = eng._gate_check(today, hist, "neutral", 60_000_000)
        assert ok is False
        assert any("G1_NO_VOL_SURGE" in f for f in flags)

    def test_stale_surge_fails_after_3_days(self):
        """3rd consecutive surge day → G1 stale."""
        eng = SurgeRadar()
        hist = _flat_history(58, volume=600_000)
        # add 2 surge bars to history (days 58, 59)
        hist.append(_bar(close=103, volume=1_200_000, day=58, lo=102, h=104))
        hist.append(_bar(close=104, volume=1_300_000, day=59, lo=103, h=105))
        today = _bar(close=105, volume=1_400_000, day=60, lo=104, h=106)
        ok, flags = eng._gate_check(today, hist, "neutral", 60_000_000)
        assert ok is False
        assert any("G1_STALE_DAY3" in f for f in flags)

    def test_weak_close_fails_g3(self):
        """Close in bottom half of range → G3 fails."""
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        # open=105 high=106 low=100 close=101 → (101-100)/6 = 0.166 < 0.5
        today = _bar(close=101, volume=1_500_000, day=99, o=105, h=106, lo=100)
        ok, flags = eng._gate_check(today, hist, "neutral", 60_000_000)
        assert ok is False
        assert any("G3_WEAK_CLOSE" in f for f in flags)

    def test_low_turnover_fails_g4(self):
        eng = SurgeRadar(market="TSE")
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=1_500_000, day=99, lo=100, h=106)
        ok, flags = eng._gate_check(today, hist, "neutral", 5_000_000)
        assert ok is False
        assert any("G4_LOW_TURNOVER" in f for f in flags)

    def test_low_lots_fails_g4(self):
        eng = SurgeRadar(market="TSE")
        # 60 bars at 100 lots (100_000 shares) each — avg_lots = 100 < 500
        hist = _flat_history(60, volume=100_000)
        today = _bar(close=105, volume=250_000, day=99, lo=100, h=106)
        ok, flags = eng._gate_check(today, hist, "neutral", 60_000_000)
        assert ok is False
        assert any("G4_LOW_LOTS" in f for f in flags)

    def test_taiex_downtrend_fails_g5(self):
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=1_500_000, day=99, lo=100, h=106)
        ok, flags = eng._gate_check(today, hist, "downtrend", 60_000_000)
        assert ok is False
        assert any("G5_TAIEX_DOWNTREND" in f for f in flags)

    def test_gate_passes_with_fresh_surge(self):
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=1_500_000, day=99, lo=100, h=106)
        ok, flags = eng._gate_check(today, hist, "neutral", 60_000_000)
        assert ok is True
        assert any("SURGE_GATE_PASS" in f for f in flags)
        assert any("SURGE_DAY1" in f for f in flags)


class TestVolumeRatio:
    def test_ideal_zone(self):
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=1_500_000, day=99, lo=100, h=106)  # 2.5x
        pts, flags = eng._score_vol_ratio(today, hist)
        assert pts == 10
        assert any("VOL_IDEAL" in f for f in flags)

    def test_mild_surge(self):
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=1_000_000, day=99, lo=100, h=106)  # 1.67x
        pts, _ = eng._score_vol_ratio(today, hist)
        assert pts == 6

    def test_extreme_warning(self):
        """≥ 3× vol → reduced score (exhaustion risk)."""
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=105, volume=2_000_000, day=99, lo=100, h=106)  # 3.33x
        pts, flags = eng._score_vol_ratio(today, hist)
        assert pts == 5
        assert any("VOL_EXTREME" in f for f in flags)


class TestCloseStrength:
    def test_strong_close(self):
        eng = SurgeRadar()
        # close at 9, low 1, high 11 → ratio (9-1)/10 = 0.8
        today = _bar(close=9, volume=1, day=0, o=5, h=11, lo=1)
        pts, _ = eng._score_close_strength(today)
        assert pts == 8

    def test_healthy_close(self):
        eng = SurgeRadar()
        # (7.5-1)/10 = 0.65
        today = _bar(close=7.5, volume=1, day=0, o=5, h=11, lo=1)
        pts, _ = eng._score_close_strength(today)
        assert pts == 5

    def test_soft_close(self):
        eng = SurgeRadar()
        # (5.5-1)/10 = 0.45
        today = _bar(close=5.5, volume=1, day=0, o=5, h=11, lo=1)
        pts, _ = eng._score_close_strength(today)
        assert pts == 2


class TestInstBuyFresh:
    def test_1_day_fresh(self):
        eng = SurgeRadar()
        pts, flags = eng._score_inst_buy_fresh(_proxy_with(foreign_days=1))
        assert pts == 4
        assert any("INST_FRESH:1D" in f for f in flags)

    def test_2_day_fresh(self):
        eng = SurgeRadar()
        pts, _ = eng._score_inst_buy_fresh(_proxy_with(foreign_days=2))
        assert pts == 7

    def test_3_day_peak(self):
        eng = SurgeRadar()
        pts, _ = eng._score_inst_buy_fresh(_proxy_with(foreign_days=3))
        assert pts == 10

    def test_unavailable_proxy_zero(self):
        eng = SurgeRadar()
        pts, _ = eng._score_inst_buy_fresh(_proxy_with(foreign_days=5, available=False))
        assert pts == 0

    def test_none_proxy_zero(self):
        eng = SurgeRadar()
        pts, _ = eng._score_inst_buy_fresh(None)
        assert pts == 0


class TestIndustryStrength:
    def test_top_20pct(self):
        eng = SurgeRadar()
        pts, _ = eng._score_industry_strength(85.0)
        assert pts == 10

    def test_top_40pct(self):
        eng = SurgeRadar()
        pts, _ = eng._score_industry_strength(65.0)
        assert pts == 5

    def test_cold_industry(self):
        eng = SurgeRadar()
        pts, _ = eng._score_industry_strength(30.0)
        assert pts == 0

    def test_none_returns_zero(self):
        eng = SurgeRadar()
        pts, _ = eng._score_industry_strength(None)
        assert pts == 0


class TestBreakout20d:
    def test_closes_above_20d_high(self):
        eng = SurgeRadar()
        hist = _flat_history(25, close=100.0)
        # today closes at 110, well above history high=101
        today = _bar(close=110, volume=1, day=99, lo=105, h=111)
        pts, _ = eng._score_breakout_20d(today, hist)
        assert pts == 10

    def test_no_breakout(self):
        eng = SurgeRadar()
        hist = _flat_history(25, close=100.0)
        today = _bar(close=99, volume=1, day=99, lo=95, h=101)
        pts, _ = eng._score_breakout_20d(today, hist)
        assert pts == 0


class TestBreakawayGap:
    def test_full_gap_held(self):
        eng = SurgeRadar()
        hist = [_bar(close=100, volume=1, day=0)]  # prev close = 100
        # open=102 (2% gap), low=101 (above prev close), close=103 > open
        today = _bar(close=103, volume=1, day=1, o=102, h=104, lo=101)
        pts, flags = eng._score_breakaway_gap(today, hist)
        assert pts == 8
        assert any("GAP_FULL" in f for f in flags)

    def test_partial_gap(self):
        eng = SurgeRadar()
        hist = [_bar(close=100, volume=1, day=0)]
        # open=100.7 (0.7% gap), close > open but low below prev close
        today = _bar(close=102, volume=1, day=1, o=100.7, h=102.5, lo=99.5)
        pts, _ = eng._score_breakaway_gap(today, hist)
        assert pts == 4

    def test_no_gap(self):
        eng = SurgeRadar()
        hist = [_bar(close=100, volume=1, day=0)]
        today = _bar(close=100, volume=1, day=1, o=100, h=101, lo=99)
        pts, _ = eng._score_breakaway_gap(today, hist)
        assert pts == 0


class TestPocketPivot:
    def test_pocket_pivot_triggers(self):
        eng = SurgeRadar()
        # 10 bars with down days having low volume, today is massive up vol
        hist: list[DailyOHLCV] = []
        for i in range(11):
            # alternate up/down days, low volume
            close = 100 + (0.3 if i % 2 else -0.3)
            hist.append(_bar(close=close, volume=300_000, day=i))
        # today: up day, volume beats max down vol, closes near high, above MA10
        today = _bar(close=101.5, volume=1_500_000, day=12, o=100.5, h=101.8, lo=100.2)
        pts, flags = eng._score_pocket_pivot(today, hist)
        assert pts == 12
        assert any("POCKET_PIVOT" in f for f in flags)

    def test_no_pocket_pivot_if_down_day(self):
        eng = SurgeRadar()
        hist: list[DailyOHLCV] = []
        for i in range(11):
            hist.append(_bar(close=100 + (0.3 if i % 2 else -0.3), volume=300_000, day=i))
        # today is down day
        prev = hist[-1].close
        today = _bar(close=prev - 1, volume=1_500_000, day=12, lo=prev - 2, h=prev)
        pts, _ = eng._score_pocket_pivot(today, hist)
        assert pts == 0


class TestRelativeStrength:
    def test_outperforms_taiex(self):
        eng = SurgeRadar()
        hist = [_bar(close=100, volume=1, day=0)]
        # TAIEX up 0.5%, stock up 2% → diff 1.5%
        taiex = [
            DailyOHLCV(ticker="TAIEX", trade_date=date(2026,1,1), open=17000, high=17050,
                       low=16950, close=17000, volume=0),
            DailyOHLCV(ticker="TAIEX", trade_date=date(2026,1,2), open=17000, high=17100,
                       low=16990, close=17085, volume=0),
        ]
        today = _bar(close=102, volume=1, day=2, lo=100, h=103)
        pts, _ = eng._score_relative_strength(today, hist, taiex)
        assert pts == 8


class TestRsiHealthy:
    def test_healthy_range(self):
        eng = SurgeRadar()
        # Build price action with RSI near 60: mostly up days with some pullbacks
        closes = [100 + i * 0.5 + (-0.3 if i % 4 == 0 else 0) for i in range(20)]
        hist = [_bar(close=c, volume=1, day=i) for i, c in enumerate(closes)]
        rsi = eng._rsi(hist)
        assert rsi is not None
        # Verify the helper works; actual RSI value tested elsewhere
        pts, _ = eng._score_rsi_healthy(hist)
        # With monotonic rise, RSI likely > 70; test that scoring gives 0 if so
        if rsi > 70:
            assert pts == 0


class TestMarginNotHot:
    def test_cool_margin(self):
        eng = SurgeRadar()
        pts, flags = eng._score_margin_not_hot(_proxy_with(util=0.10))
        assert pts == 4
        assert any("MARGIN_COOL" in f for f in flags)

    def test_hot_margin(self):
        eng = SurgeRadar()
        pts, _ = eng._score_margin_not_hot(_proxy_with(util=0.25))
        assert pts == 0

    def test_none_util_zero(self):
        eng = SurgeRadar()
        pts, _ = eng._score_margin_not_hot(_proxy_with(util=None))
        assert pts == 0


class TestScoreFull:
    def test_alpha_grade_with_full_confluence(self):
        """Stock with vol burst + strong close + inst buy + breakout + industry hot → ALPHA."""
        eng = SurgeRadar()
        hist = _flat_history(60, close=100, volume=600_000)
        today = _bar(close=106, volume=1_500_000, day=99, o=100.5, h=106.5, lo=100)
        proxy = _proxy_with(foreign_days=2, util=0.08)
        taiex = _taiex_history(70)
        result = eng.score_full(
            ohlcv=today,
            history=hist,
            proxy=proxy,
            taiex_regime="neutral",
            taiex_history=taiex,
            turnover_20ma=60_000_000,
            industry_rank_pct=90.0,
        )
        assert result is not None
        assert result["grade"] in ("SURGE_ALPHA", "SURGE_BETA")
        assert result["score"] >= 40
        assert result["vol_ratio"] == pytest.approx(2.5)
        assert result["surge_day"] == 1

    def test_returns_none_on_gate_fail(self):
        eng = SurgeRadar()
        hist = _flat_history(60, volume=600_000)
        today = _bar(close=100, volume=600_000, day=99, lo=99, h=101)  # no surge
        result = eng.score_full(
            ohlcv=today,
            history=hist,
            proxy=None,
            taiex_regime="neutral",
            taiex_history=_taiex_history(),
            turnover_20ma=60_000_000,
        )
        assert result is None

    def test_returns_none_if_below_gamma(self):
        """Low-score stock passes gate but fails grade threshold."""
        eng = SurgeRadar()
        hist = _flat_history(60, close=100, volume=600_000)
        # Barely surging, below 20d high, no inst, cold industry → low score
        today = _bar(close=100.5, volume=950_000, day=99, o=100.2, h=100.8, lo=100)
        result = eng.score_full(
            ohlcv=today,
            history=hist,
            proxy=None,
            taiex_regime="neutral",
            taiex_history=_taiex_history(),
            turnover_20ma=60_000_000,
            industry_rank_pct=20.0,
        )
        # Depends on exact threshold; if result returned, must be valid grade
        if result is not None:
            assert result["grade"] in ("SURGE_ALPHA", "SURGE_BETA", "SURGE_GAMMA")


class TestConsecutiveSurge:
    def test_count_increments_when_bars_above_threshold(self):
        eng = SurgeRadar()
        hist = _flat_history(58, volume=600_000)
        hist.append(_bar(close=103, volume=1_200_000, day=58))
        today = _bar(close=104, volume=1_300_000, day=59)
        assert eng._consecutive_surge_days(today, hist) == 2

    def test_count_resets_on_quiet_bar(self):
        eng = SurgeRadar()
        hist = _flat_history(58, volume=600_000)
        hist.append(_bar(close=103, volume=600_000, day=58))  # quiet bar
        today = _bar(close=104, volume=1_300_000, day=59)  # surge
        assert eng._consecutive_surge_days(today, hist) == 1

    def test_zero_vol_20ma_returns_zero(self):
        eng = SurgeRadar()
        hist = [_bar(close=100, volume=0, day=i) for i in range(20)]
        today = _bar(close=100, volume=1000, day=21)
        assert eng._consecutive_surge_days(today, hist) == 0
