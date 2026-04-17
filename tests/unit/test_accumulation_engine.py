from __future__ import annotations
from datetime import date, timedelta
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"))
import pytest
from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.accumulation_engine import AccumulationEngine


def _make_history(n: int, base_close: float = 100.0, base_vol: int = 10_000,
                  flat: bool = False, trending_up: bool = False) -> list[DailyOHLCV]:
    result = []
    d = date(2024, 1, 2)
    for i in range(n):
        close = base_close if flat else (base_close + i * 0.5 if trending_up else base_close + (i % 3) * 0.2)
        result.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=i),
            open=close - 0.3, high=close + 0.8, low=close - 0.8, close=close, volume=base_vol,
        ))
    return result


def test_obv_slope_positive_on_up_days():
    hist = _make_history(20, trending_up=True, base_vol=10_000)
    slope = AccumulationEngine._obv_slope(hist)
    assert slope is not None
    assert slope > 0


def test_obv_slope_returns_none_insufficient_history():
    hist = _make_history(4)
    assert AccumulationEngine._obv_slope(hist) is None


def test_atr_positive():
    hist = _make_history(20)
    atr = AccumulationEngine._atr(hist)
    assert atr is not None and atr > 0


def test_atr_percentile_low_for_compressed():
    # Needs >= period(14) + window(252) = 266 bars minimum → use 300
    hist_high_vol = _make_history(300, base_vol=10_000)
    # Replace last 14 bars with tiny-range bars (cannot mutate Pydantic v2 models in-place)
    compressed = [
        DailyOHLCV(
            ticker=bar.ticker,
            trade_date=bar.trade_date,
            open=bar.close - 0.05,
            high=bar.close + 0.1,
            low=bar.close - 0.1,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in hist_high_vol[-14:]
    ]
    hist = hist_high_vol[:-14] + compressed
    pct = AccumulationEngine._atr_percentile(hist)
    assert pct is not None and pct < 30.0


def test_atr_percentile_none_insufficient():
    # 266 bars minimum required; 260 < 266 → None
    hist = _make_history(260)
    assert AccumulationEngine._atr_percentile(hist) is None


def test_kd_d_returns_list_of_values():
    hist = _make_history(60)
    vals = AccumulationEngine._kd_d(hist)
    assert vals is not None and len(vals) == 5


def test_kd_d_none_insufficient():
    hist = _make_history(5)
    assert AccumulationEngine._kd_d(hist) is None


def test_gate_passes_uptrend_not_broken_out():
    hist = _make_history(80, trending_up=True)
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="neutral", turnover_20ma=30_000_000)
    assert passed is True
    assert "ACCUM_GATE_PASS" in flags


def test_gate_fails_already_broken_out():
    # Flat consolidation: prior bars have resistance at high=102.
    # Last 10 bars close at 107, which is > 102 × 1.03 = 105.06 — breakout detected.
    d = date(2024, 1, 2)
    base = [
        DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=i),
            open=100.0, high=102.0, low=99.0, close=100.5, volume=10_000,
        )
        for i in range(70)
    ]
    breakout = [
        DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=70 + i),
            open=105.0, high=102.5, low=104.0, close=107.0, volume=20_000,
        )
        for i in range(10)
    ]
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(base + breakout, taiex_regime="neutral", turnover_20ma=30_000_000)
    assert passed is False
    assert any("G2_ALREADY_BROKE" in f for f in flags)


def test_gate_fails_downtrend_regime():
    hist = _make_history(80, trending_up=True)
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="downtrend", turnover_20ma=30_000_000)
    assert passed is False
    assert "G3_TAIEX_DOWNTREND" in flags


def test_gate_fails_low_liquidity():
    hist = _make_history(80, trending_up=True)
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="neutral", turnover_20ma=5_000_000)
    assert passed is False
    assert any("G4_LOW_LIQUIDITY" in f for f in flags)


def test_gate_fails_ma20_below_ma60():
    # Build declining price history so MA20 < MA60
    from datetime import date, timedelta
    from taiwan_stock_agent.domain.models import DailyOHLCV
    d = date(2024, 1, 2)
    hist = [
        DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=i),
            open=100.0 - i*0.5, high=100.0 - i*0.5 + 0.3,
            low=100.0 - i*0.5 - 0.3, close=100.0 - i*0.5, volume=10_000
        )
        for i in range(80)
    ]
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="neutral", turnover_20ma=30_000_000)
    assert passed is False
    assert any("G1" in f for f in flags)


# ===========================================================================
# Task 3: Dimension A — Compression Pattern
# ===========================================================================

def test_score_bb_compression_extreme():
    # Build 300-bar history where last 20 bars are very compressed (tiny range)
    hist = _make_history(300, trending_up=True)
    compressed = [
        bar.model_copy(update={"high": bar.close + 0.05, "low": bar.close - 0.05})
        for bar in hist[-20:]
    ]
    hist_mod = hist[:-20] + compressed
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_bb_compression(hist_mod)
    assert pts >= 10


def test_score_volume_dryup_extreme():
    hist = _make_history(30, base_vol=10_000)
    low_vol = [bar.model_copy(update={"volume": 3_000}) for bar in hist[-5:]]
    hist_mod = hist[:-5] + low_vol
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_volume_dryup(hist_mod)
    assert pts == 15


def test_score_volume_dryup_moderate():
    hist = _make_history(30, base_vol=10_000)
    mod_vol = [bar.model_copy(update={"volume": 8_000}) for bar in hist[-5:]]
    hist_mod = hist[:-5] + mod_vol
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_volume_dryup(hist_mod)
    assert pts == 8


def test_score_consolidation_tight():
    # 20 days with spread < 5%: high=101, low=99.5 → spread = 1.5/99.5 ≈ 1.5% < 5%
    d = date(2024, 1, 2)
    hist = [
        DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                   open=100.0, high=101.0, low=99.5, close=100.0, volume=10_000)
        for i in range(30)
    ]
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_consolidation_range(hist)
    assert pts == 15


def test_score_inside_bars():
    # Create 6 bars where the last bar is inside the second-to-last bar
    d = date(2024, 1, 2)
    hist = [
        DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                   open=100.0, high=102.0, low=98.0, close=100.0, volume=10_000)
        for i in range(5)
    ]
    # Last bar is inside: high=101.5, low=98.5 (inside prev 102/98)
    hist.append(DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=5),
                            open=100.0, high=101.5, low=98.5, close=100.0, volume=10_000))
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_inside_bars(hist)
    assert pts >= 2


# ===========================================================================
# Task 4: Dimension B — Technical Confirmation
# ===========================================================================

def test_score_ma_convergence_tight():
    hist = _make_history(30, flat=True, base_close=100.0)
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_ma_convergence(hist)
    assert pts == 10  # flat history → MA5/10/20 all equal → gap=0 < 2%


def test_score_obv_trend_rising_price_flat():
    # Build flat-ish history (< 2% price change in last 5 bars)
    # with rising volume on up days to generate positive OBV slope
    d = date(2024, 1, 2)
    # 25 bars flat at 100, then last 5 bars inch up with high volume
    hist = [
        DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                   open=100.0, high=100.8, low=99.2, close=100.0, volume=5_000)
        for i in range(25)
    ]
    for i in range(5):
        close = 100.0 + i * 0.1  # tiny up moves, total < 2%
        hist.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=25 + i),
            open=close - 0.05, high=close + 0.3, low=close - 0.3, close=close, volume=20_000
        ))
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_obv_trend(hist)
    assert pts == 8


def test_score_kd_low_flat():
    # Close near the bottom of range → KD-D will be low
    d = date(2024, 1, 2)
    hist = [
        DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                   open=16.0, high=25.0, low=15.0, close=16.0, volume=10_000)
        for i in range(60)
    ]
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_kd_low_flat(hist)
    assert pts == 7


def test_score_close_above_midline():
    hist = _make_history(30, trending_up=True, base_close=100.0)
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_close_above_midline(hist)
    assert pts == 5


# ===========================================================================
# Task 5: Dimension C — Chip Behavior
# ===========================================================================

def _make_proxy(foreign_days: int = 0, trust_days: int = 0):
    from taiwan_stock_agent.domain.models import TWSEChipProxy
    return TWSEChipProxy(
        ticker="TEST", trade_date=date(2025, 1, 1),
        foreign_net_buy=10_000 if foreign_days > 0 else 0,
        trust_net_buy=5_000 if trust_days > 0 else 0,
        dealer_net_buy=0,
        avg_20d_volume=10_000,
        foreign_consecutive_buy_days=foreign_days,
        trust_consecutive_buy_days=trust_days,
        dealer_consecutive_buy_days=0,
        is_available=True,
    )


def test_score_inst_consec_prime():
    proxy = _make_proxy(foreign_days=5)
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_institutional_consec(proxy)
    assert pts == 20


def test_score_inst_consec_mid():
    proxy = _make_proxy(foreign_days=3)
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_institutional_consec(proxy)
    assert pts == 12


def test_score_updown_volume_structure():
    d = date(2025, 1, 2)
    prev_close = 100.0
    hist = []
    for i in range(10):
        close = prev_close + (1 if i % 2 == 0 else -0.5)
        vol = 20_000 if close > prev_close else 5_000
        hist.append(DailyOHLCV(
            ticker="T", trade_date=d + timedelta(days=i),
            open=prev_close, high=close + 0.5, low=close - 0.5, close=close, volume=vol
        ))
        prev_close = close
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_updown_volume(hist)
    assert pts == 8


def test_score_prior_advance():
    hist = _make_history(70, trending_up=True, base_close=100.0)
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_prior_advance(hist)
    assert pts == 5  # close ~135 / min60 >= 1.15


# ===========================================================================
# Task 6: score_full() + _grade()
# ===========================================================================

def test_score_full_prime_grade():
    """A well-constructed accumulation setup should return COIL_PRIME or COIL_MATURE.

    Setup: 270 bars trending up from 50→85, then last 30 bars compressed at 85±0.1.
    MA20 ≈ 85, MA60 ≈ rising into 85 — MA20 > MA60 holds.
    Close (85) < 60d high × 1.03 — not yet broken out.
    """
    d = date(2024, 1, 2)
    # 270 bars trending up from 50 to ~85 (step ~0.13/bar)
    base_bars: list[DailyOHLCV] = []
    for i in range(270):
        close = 50.0 + i * (35.0 / 270)
        base_bars.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=i),
            open=close - 0.3, high=close + 0.8, low=close - 0.8, close=close, volume=10_000
        ))
    # Last 30 bars compressed at 85.0 with very tight range
    compress_bars: list[DailyOHLCV] = []
    for i in range(30):
        compress_bars.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=270 + i),
            open=85.0, high=85.2, low=84.8, close=85.0, volume=3_000
        ))
    hist_mod = base_bars + compress_bars
    proxy = _make_proxy(foreign_days=5)
    taiex = _make_history(30, trending_up=True, base_close=18_000.0)
    eng = AccumulationEngine(market="TSE")
    result = eng.score_full(
        history=hist_mod, proxy=proxy,
        taiex_regime="neutral", taiex_history=taiex,
        turnover_20ma=30_000_000,
    )
    assert result is not None
    assert result["grade"] in ("COIL_PRIME", "COIL_MATURE")
    assert result["score"] >= 50
    assert "flags" in result
    assert "score_breakdown" in result


def test_score_full_gate_fail_returns_none():
    # Flat history → MA20 will equal MA60 → G1 fails
    hist = _make_history(80, flat=True, base_close=100.0)
    eng = AccumulationEngine(market="TSE")
    result = eng.score_full(
        history=hist, proxy=None,
        taiex_regime="neutral", taiex_history=[],
        turnover_20ma=30_000_000,
    )
    assert result is None


def test_score_full_grade_thresholds():
    eng = AccumulationEngine(market="TSE")
    assert eng._grade(72) == "COIL_PRIME"
    assert eng._grade(55) == "COIL_MATURE"
    assert eng._grade(40) == "COIL_EARLY"
    assert eng._grade(20) is None


# ── _weeks_consolidating tests ────────────────────────────────────────────────

def test_weeks_consolidating_flat():
    from coil_scan import _weeks_consolidating
    hist = _make_history(30, flat=True, base_close=100.0)
    weeks = _weeks_consolidating(hist)
    assert weeks >= 4  # 30 flat sessions → at least 6 weeks (30//5=6)


def test_weeks_consolidating_volatile_breaks_early():
    from coil_scan import _weeks_consolidating
    # Build flat history, then replace a bar near the end with a spike (outside 20d spread)
    hist = _make_history(30, flat=True, base_close=100.0)
    spike = hist[-5].model_copy(update={"close": 115.0})  # outside 20d spread
    hist_mod = hist[:-5] + [spike] + hist[-4:]
    weeks = _weeks_consolidating(hist_mod)
    assert weeks <= 1
