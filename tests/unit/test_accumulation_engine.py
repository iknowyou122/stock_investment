from __future__ import annotations
from datetime import date, timedelta
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
    hist = _make_history(80, trending_up=True)
    # Replace last 10 bars with very high closes (above 60d high × 1.03)
    modified = hist[:-10] + [
        bar.model_copy(update={"close": 9999.0, "high": 9999.0})
        for bar in hist[-10:]
    ]
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(modified, taiex_regime="neutral", turnover_20ma=30_000_000)
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
