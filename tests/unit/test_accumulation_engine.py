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
    # Make last 14 bars have tiny range
    for bar in hist_high_vol[-14:]:
        bar.high = bar.close + 0.1
        bar.low = bar.close - 0.1
    pct = AccumulationEngine._atr_percentile(hist_high_vol)
    assert pct is not None and pct < 30.0


def test_atr_percentile_none_insufficient():
    # 266 bars minimum required; 260 < 266 → None
    hist = _make_history(260)
    assert AccumulationEngine._atr_percentile(hist) is None


def test_kd_d_returns_list_of_values():
    hist = _make_history(60)
    vals = AccumulationEngine._kd_d(hist)
    assert vals is not None and len(vals) >= 3


def test_kd_d_none_insufficient():
    hist = _make_history(5)
    assert AccumulationEngine._kd_d(hist) is None
