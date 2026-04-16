# Accumulation Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a parallel accumulation-detection track to `make plan` that surfaces pre-breakout stocks (COIL_PRIME/MATURE/EARLY) in a separate CSV, terminal table, and Telegram message — completely independent of the existing momentum engine.

**Architecture:** New `AccumulationEngine` class with its own 4-gate + 15-factor scoring logic; `coil_scan.py` standalone runner; `batch_plan.py` gains a Pass 2 that calls `coil_scan` after momentum scan completes using shared OHLCV cache; `bot.py` gets a 蓄積雷達 display block reading the latest coil CSV.

**Tech Stack:** Python 3.11+, pandas (already in requirements), Rich (already in requirements), existing `FinMindClient` + `ChipProxyFetcher` shared instances, pytest for TDD.

**Spec:** `docs/superpowers/specs/2026-04-16-accumulation-engine-design.md`

---

## File Map

| Status | File | Role |
|--------|------|------|
| CREATE | `src/taiwan_stock_agent/domain/accumulation_engine.py` | Engine class: gates + 15 scoring factors + grade output |
| CREATE | `tests/unit/test_accumulation_engine.py` | Unit tests: indicators, gates, each factor, grade thresholds |
| CREATE | `scripts/coil_scan.py` | Standalone CLI: load tickers, run AccumulationEngine, save coil CSV, Rich table |
| CREATE | `config/accumulation_params.json` | Tunable weights and thresholds |
| CREATE | `scripts/coil_backtest.py` | Replay engine over historical data, compute win rates |
| CREATE | `scripts/coil_factor_report.py` | Factor lift analysis |
| CREATE | `scripts/optimize_coil.py` | Grid search + walk-forward for accumulation_params.json |
| MODIFY | `scripts/batch_plan.py` | Add Pass 2 coil scan call + render 蓄積雷達 table |
| MODIFY | `scripts/bot.py` | Add 蓄積雷達 block to Bot Status panel |
| MODIFY | `Makefile` | Add `coil`, `coil-backtest`, `coil-factor-report`, `optimize-coil` targets |

---

## Task 1: Indicator Implementations

**Files:**
- Create: `src/taiwan_stock_agent/domain/accumulation_engine.py` (skeleton + indicators only)
- Create: `tests/unit/test_accumulation_engine.py`

Implement four static indicator methods that don't exist in the codebase. All take `list[DailyOHLCV]` (import from `taiwan_stock_agent.domain.models`).

- [ ] **Step 1: Create test file skeleton**

```python
# tests/unit/test_accumulation_engine.py
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
```

- [ ] **Step 2: Write failing tests for OBV slope**

```python
def test_obv_slope_positive_on_up_days():
    # All up days with volume → OBV slope should be positive
    hist = _make_history(20, trending_up=True, base_vol=10_000)
    slope = AccumulationEngine._obv_slope(hist)
    assert slope is not None
    assert slope > 0

def test_obv_slope_returns_none_insufficient_history():
    hist = _make_history(4)
    assert AccumulationEngine._obv_slope(hist) is None
```

- [ ] **Step 3: Create engine skeleton + implement `_obv_slope`**

```python
# src/taiwan_stock_agent/domain/accumulation_engine.py
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
import pandas as pd
from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "accumulation_params.json"


class AccumulationEngine:

    @staticmethod
    def _obv_slope(history: list[DailyOHLCV]) -> float | None:
        """5-day linear slope of OBV."""
        if len(history) < 5:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        obv = 0.0
        obvs = []
        prev_close = sorted_h[0].close
        for bar in sorted_h:
            if bar.close > prev_close:
                obv += bar.volume
            elif bar.close < prev_close:
                obv -= bar.volume
            obvs.append(obv)
            prev_close = bar.close
        series = pd.Series(obvs[-5:])
        x = pd.Series(range(5), dtype=float)
        slope = (5 * (x * series).sum() - x.sum() * series.sum()) / (5 * (x**2).sum() - x.sum()**2)
        return float(slope)
```

- [ ] **Step 4: Run OBV tests**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/pytest tests/unit/test_accumulation_engine.py::test_obv_slope_positive_on_up_days tests/unit/test_accumulation_engine.py::test_obv_slope_returns_none_insufficient_history -v
```
Expected: 2 PASSED

- [ ] **Step 5: Write + implement ATR and ATR percentile**

Add to test file:
```python
def test_atr_positive():
    hist = _make_history(20)
    atr = AccumulationEngine._atr(hist)
    assert atr is not None and atr > 0

def test_atr_percentile_low_for_compressed():
    # Build history where recent ATR is much lower than historical
    # Needs >= period(14) + window(252) = 266 bars minimum → use 300
    hist_high_vol = _make_history(300, base_vol=10_000)
    # Make last 14 bars have tiny range
    for bar in hist_high_vol[-14:]:
        bar.high = bar.close + 0.1
        bar.low = bar.close - 0.1
    pct = AccumulationEngine._atr_percentile(hist_high_vol)
    assert pct is not None and pct < 30.0

def test_atr_percentile_none_insufficient():
    # 266 bars minimum required (period=14 + window=252); 260 < 266 → None
    hist = _make_history(260)
    assert AccumulationEngine._atr_percentile(hist) is None
```

Add to engine:
```python
@staticmethod
def _atr(history: list[DailyOHLCV], period: int = 14) -> float | None:
    if len(history) < period + 1:
        return None
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    trs = []
    for i in range(1, len(sorted_h)):
        prev_close = sorted_h[i-1].close
        bar = sorted_h[i]
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        trs.append(tr)
    return float(sum(trs[-period:]) / period)

@staticmethod
def _atr_percentile(history: list[DailyOHLCV], period: int = 14, window: int = 252) -> float | None:
    if len(history) < period + window:
        return None
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    trs = []
    for i in range(1, len(sorted_h)):
        prev_close = sorted_h[i-1].close
        bar = sorted_h[i]
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        trs.append(tr)
    atrs = [sum(trs[i:i+period])/period for i in range(len(trs)-period+1)]
    recent_atr = atrs[-1]
    window_atrs = atrs[-window:]
    rank = sum(1 for v in window_atrs if v < recent_atr)
    return round(float(rank) / len(window_atrs) * 100, 1)
```

- [ ] **Step 6: Write + implement KD-D (Stochastic %D)**

Add to test file:
```python
def test_kd_d_returns_list_of_values():
    hist = _make_history(60)
    vals = AccumulationEngine._kd_d(hist)
    assert vals is not None and len(vals) >= 3

def test_kd_d_none_insufficient():
    hist = _make_history(5)
    assert AccumulationEngine._kd_d(hist) is None
```

Add to engine:
```python
@staticmethod
def _kd_d(history: list[DailyOHLCV], k_period: int = 9, d_smooth: int = 3,
           lookback: int = 5) -> list[float] | None:
    """Returns last `lookback` Stochastic %D values, or None if insufficient history."""
    min_needed = k_period + d_smooth + lookback
    if len(history) < min_needed:
        return None
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    k_vals = []
    for i in range(k_period - 1, len(sorted_h)):
        window = sorted_h[i - k_period + 1: i + 1]
        low_k = min(b.low for b in window)
        high_k = max(b.high for b in window)
        rng = high_k - low_k
        k = ((sorted_h[i].close - low_k) / rng * 100) if rng > 0 else 50.0
        k_vals.append(k)
    d_vals = []
    for i in range(d_smooth - 1, len(k_vals)):
        d_vals.append(sum(k_vals[i - d_smooth + 1: i + 1]) / d_smooth)
    return [round(v, 2) for v in d_vals[-lookback:]] if len(d_vals) >= lookback else None
```

- [ ] **Step 7: Run all indicator tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -v
```
Expected: all PASSED

- [ ] **Step 8: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py tests/unit/test_accumulation_engine.py
git commit -m "feat: AccumulationEngine skeleton + OBV/ATR/KD indicator implementations"
```

---

## Task 2: Gate Layer

**Files:**
- Modify: `src/taiwan_stock_agent/domain/accumulation_engine.py`
- Modify: `tests/unit/test_accumulation_engine.py`

Implement the 4-gate hard filter. Gates check: trend (MA20>MA60, slope), not-yet-broken-out, market regime, liquidity.

- [ ] **Step 1: Write failing gate tests**

```python
def test_gate_passes_uptrend_not_broken_out():
    # MA20 > MA60, MA20 rising, close well below 60d high
    hist = _make_history(80, trending_up=True)
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="neutral", turnover_20ma=30_000_000)
    assert passed is True
    assert "ACCUM_GATE_PASS" in flags

def test_gate_fails_already_broken_out():
    hist = _make_history(80, trending_up=True)
    # Set last 10 closes above 60d high × 1.03
    for bar in hist[-10:]:
        bar.close = bar.high = 9999.0
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="neutral", turnover_20ma=30_000_000)
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
    hist = _make_history(80, trending_up=False, flat=True, base_close=100.0)
    # Force MA20 below MA60 by having decreasing prices
    for i, bar in enumerate(hist):
        bar.close = bar.high = bar.low = bar.open = 100.0 - i * 0.5
    eng = AccumulationEngine(market="TSE")
    passed, flags = eng._gate_check(hist, taiex_regime="neutral", turnover_20ma=30_000_000)
    assert passed is False
    assert any("G1" in f for f in flags)
```

- [ ] **Step 2: Implement `__init__` and `_gate_check`**

```python
class AccumulationEngine:
    def __init__(self, market: str = "TSE"):
        self._market = market
        self._params = self._load_params()

    @staticmethod
    def _load_params() -> dict:
        try:
            return json.loads(_PARAMS_PATH.read_text())
        except Exception:
            return {}

    def _gate_check(
        self,
        history: list[DailyOHLCV],
        taiex_regime: str,
        turnover_20ma: float,
    ) -> tuple[bool, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        flags: list[str] = []

        # G1: MA20 > MA60 and MA20 slope >= 0
        if len(closes) < 60:
            return False, ["ACCUM_SKIP:INSUFFICIENT_HISTORY"]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        if ma20 <= ma60:
            return False, ["ACCUM_FAIL:G1_MA20_LE_MA60"]
        if len(closes) >= 25:
            ma20_5d_ago = sum(closes[-25:-5]) / 20
            if ma20 < ma20_5d_ago:
                return False, ["ACCUM_FAIL:G1_MA20_SLOPE_DOWN"]

        # G2: not yet broken out (max of last 10 closes < 60d high × 1.03)
        sixty_day_high = max(closes[-60:])
        last10_closes = closes[-10:]
        if max(last10_closes) >= sixty_day_high * 1.03:
            return False, ["ACCUM_FAIL:G2_ALREADY_BROKE"]

        # G3: market regime
        if taiex_regime == "downtrend":
            return False, ["G3_TAIEX_DOWNTREND"]

        # G4: liquidity
        tse_threshold = 20_000_000
        tpex_threshold = 8_000_000
        threshold = tse_threshold if self._market == "TSE" else tpex_threshold
        if turnover_20ma < threshold:
            return False, [f"G4_LOW_LIQUIDITY:{turnover_20ma/1e6:.1f}M<{threshold/1e6:.0f}M"]

        flags.append("ACCUM_GATE_PASS")
        return True, flags
```

- [ ] **Step 3: Run gate tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -k "gate" -v
```
Expected: 5 PASSED

- [ ] **Step 4: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py tests/unit/test_accumulation_engine.py
git commit -m "feat: AccumulationEngine gate layer (G1-G4)"
```

---

## Task 3: Scoring — Dimension A (Compression Pattern)

**Files:**
- Modify: `src/taiwan_stock_agent/domain/accumulation_engine.py`
- Modify: `tests/unit/test_accumulation_engine.py`

Five factors: BB bandwidth compression, volume dry-up, price consolidation range, ATR contraction, inside bar count.

- [ ] **Step 1: Write failing tests for Dimension A factors**

```python
def test_score_bb_compression_extreme():
    # Build 300-bar history where last 20 bars are very compressed
    hist = _make_history(300, trending_up=True)
    for bar in hist[-20:]:
        bar.high = bar.close + 0.05
        bar.low = bar.close - 0.05
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_bb_compression(hist)
    assert pts >= 10  # Should trigger at minimum the 10-pt tier

def test_score_volume_dryup_extreme():
    hist = _make_history(30, base_vol=10_000)
    for bar in hist[-5:]:
        bar.volume = 3_000  # 30% of 20d avg → <70% threshold
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_volume_dryup(hist)
    assert pts == 15

def test_score_volume_dryup_moderate():
    hist = _make_history(30, base_vol=10_000)
    for bar in hist[-5:]:
        bar.volume = 8_000  # 80% of avg → <85% threshold
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_volume_dryup(hist)
    assert pts == 8

def test_score_consolidation_tight():
    hist = _make_history(30, flat=True, base_close=100.0)
    # 20d high/low spread < 5%
    for bar in hist[-20:]:
        bar.high = 101.0
        bar.low = 99.5
        bar.close = 100.0
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_consolidation_range(hist)
    assert pts == 15

def test_score_inside_bars():
    hist = _make_history(10)
    prev = hist[-2]
    # Last bar is inside prev bar
    hist[-1].high = prev.high - 0.1
    hist[-1].low = prev.low + 0.1
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_inside_bars(hist)
    assert pts >= 2
```

- [ ] **Step 2: Implement Dimension A scoring methods**

```python
def _score_bb_compression(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = pd.Series([d.close for d in sorted_h])
    if len(closes) < 20:
        return 0, []
    period, num_std, window = 20, 2.0, 252
    ma = closes.rolling(period).mean()
    std = closes.rolling(period).std(ddof=0)
    width = ((ma + num_std*std) - (ma - num_std*std)) / ma.replace(0, float("nan"))
    width_vals = width.dropna()
    if len(width_vals) < window:
        return 0, ["ACCUM_SHORT_HISTORY:" + str(len(width_vals))]
    recent = width_vals.iloc[-window:]
    current = width_vals.iloc[-1]
    pct = float((recent < current).sum()) / len(recent) * 100
    if pct < 15:
        return 20, [f"ACCUM_BB_PCT:{pct:.0f}"]
    if pct < 30:
        return 10, [f"ACCUM_BB_PCT:{pct:.0f}"]
    return 0, [f"ACCUM_BB_PCT:{pct:.0f}"]

def _score_volume_dryup(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    vols = [d.volume for d in sorted_h]
    if len(vols) < 20:
        return 0, []
    avg20 = sum(vols[-20:]) / 20
    avg5 = sum(vols[-5:]) / 5
    if avg20 <= 0:
        return 0, []
    ratio = avg5 / avg20
    if ratio < 0.70:
        return 15, [f"ACCUM_VOL_DRYUP:{ratio:.2f}"]
    if ratio < 0.85:
        return 8, [f"ACCUM_VOL_DRYUP:{ratio:.2f}"]
    return 0, [f"ACCUM_VOL_RATIO:{ratio:.2f}"]

def _score_consolidation_range(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 20:
        return 0, []
    last20 = sorted_h[-20:]
    high20 = max(d.high for d in last20)
    low20 = min(d.low for d in last20)
    if low20 <= 0:
        return 0, []
    spread = (high20 - low20) / low20
    if spread < 0.05:
        return 15, [f"ACCUM_RANGE:{spread*100:.1f}PCT"]
    if spread < 0.08:
        return 8, [f"ACCUM_RANGE:{spread*100:.1f}PCT"]
    return 0, [f"ACCUM_RANGE:{spread*100:.1f}PCT"]

def _score_atr_contraction(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    pct = self._atr_percentile(history)
    if pct is None:
        return 0, ["ACCUM_SHORT_HISTORY:ATR"]
    if pct < 20:
        return 10, [f"ACCUM_ATR_PCT:{pct:.0f}"]
    if pct < 35:
        return 5, [f"ACCUM_ATR_PCT:{pct:.0f}"]
    return 0, [f"ACCUM_ATR_PCT:{pct:.0f}"]

def _score_inside_bars(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 6:
        return 0, []
    last6 = sorted_h[-6:]
    count = 0
    for i in range(1, len(last6)):
        cur, prev = last6[i], last6[i-1]
        if cur.high <= prev.high and cur.low >= prev.low:
            count += 1
    if count >= 3:
        return 5, [f"ACCUM_INSIDE_BARS:{count}"]
    if count >= 1:
        return 2, [f"ACCUM_INSIDE_BARS:{count}"]
    return 0, []
```

- [ ] **Step 3: Run Dimension A tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -k "bb_compression or dryup or consolidation or inside" -v
```
Expected: all PASSED

- [ ] **Step 4: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py tests/unit/test_accumulation_engine.py
git commit -m "feat: AccumulationEngine Dimension A scoring (BB/vol/range/ATR/inside bars)"
```

---

## Task 4: Scoring — Dimension B (Technical Confirmation)

**Files:**
- Modify: `src/taiwan_stock_agent/domain/accumulation_engine.py`
- Modify: `tests/unit/test_accumulation_engine.py`

Four factors: MA convergence, OBV trend, KD low-range consolidation, close above BB midline.

- [ ] **Step 1: Write failing tests**

```python
def test_score_ma_convergence_tight():
    hist = _make_history(30, flat=True, base_close=100.0)
    # MA5/MA10/MA20 all near 100 — tight convergence
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_ma_convergence(hist)
    assert pts == 10  # gap < 2%

def test_score_obv_trend_rising_price_flat():
    hist = _make_history(30, flat=True, base_close=100.0)
    # Ensure up-days dominate in last 5 to make OBV slope positive
    for i, bar in enumerate(hist[-5:]):
        bar.close = 100.0 + i * 0.1  # tiny up moves (< 2% total)
        bar.volume = 20_000  # high volume on up days
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_obv_trend(hist)
    assert pts == 8

def test_score_kd_low_flat():
    # Build history where stochastic is locked low and flat
    hist = _make_history(60, flat=True, base_close=20.0)
    # Close near the low of range → low KD
    for bar in hist:
        bar.high = 25.0
        bar.low = 15.0
        bar.close = 16.0  # near low → KD-D will be ~10
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_kd_low_flat(hist)
    assert pts == 7

def test_score_close_above_midline():
    hist = _make_history(30, trending_up=True, base_close=100.0)
    # Last close above MA20
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_close_above_midline(hist)
    assert pts == 5
```

- [ ] **Step 2: Implement Dimension B methods**

```python
def _score_ma_convergence(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = [d.close for d in sorted_h]
    if len(closes) < 20:
        return 0, []
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ref = closes[-1]
    if ref <= 0:
        return 0, []
    gap = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ref * 100
    if gap < 2.0:
        return 10, [f"ACCUM_MA_GAP:{gap:.1f}PCT"]
    if gap < 4.0:
        return 5, [f"ACCUM_MA_GAP:{gap:.1f}PCT"]
    return 0, [f"ACCUM_MA_GAP:{gap:.1f}PCT"]

def _score_obv_trend(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    if len(history) < 10:
        return 0, []
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    # Check price flatness (abs 5-day return < 2%)
    closes = [d.close for d in sorted_h]
    price_return = abs(closes[-1] / closes[-6] - 1) if closes[-6] > 0 else 1.0
    if price_return >= 0.02:
        return 0, []
    slope = self._obv_slope(sorted_h)
    if slope is not None and slope > 0:
        return 8, ["ACCUM_OBV_RISING"]
    return 0, []

def _score_kd_low_flat(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    vals = self._kd_d(history)
    if vals is None or len(vals) < 3:
        return 0, []
    latest_d = vals[-1]
    flatness = max(vals[-3:]) - min(vals[-3:])
    if latest_d < 30 and flatness < 5.0:
        return 7, [f"ACCUM_KD_D:{latest_d:.1f}"]
    return 0, []

def _score_close_above_midline(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = [d.close for d in sorted_h]
    if len(closes) < 20:
        return 0, []
    ma20 = sum(closes[-20:]) / 20
    if closes[-1] > ma20:
        return 5, ["ACCUM_ABOVE_MIDLINE"]
    return 0, []
```

- [ ] **Step 3: Run Dimension B tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -k "ma_convergence or obv_trend or kd_low or midline" -v
```
Expected: all PASSED

- [ ] **Step 4: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py tests/unit/test_accumulation_engine.py
git commit -m "feat: AccumulationEngine Dimension B scoring (MA convergence/OBV/KD/midline)"
```

---

## Task 5: Scoring — Dimension C (Chip Behavior)

**Files:**
- Modify: `src/taiwan_stock_agent/domain/accumulation_engine.py`
- Modify: `tests/unit/test_accumulation_engine.py`

Six factors: institutional consecutive buy days, institutional net buy proxy, up/down volume structure, market-relative strength, price proximity to resistance, prior advance.

- [ ] **Step 1: Write failing tests**

```python
from taiwan_stock_agent.domain.models import TWSEChipProxy

def _make_proxy(foreign_days: int = 0, trust_days: int = 0) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="TEST", trade_date=date(2025, 1, 1),
        foreign_net_buy=10000 if foreign_days > 0 else 0,
        trust_net_buy=5000 if trust_days > 0 else 0,
        dealer_net_buy=0, avg_20d_volume=10000,  # NOTE: field is avg_20d_volume not avg_20d_vol
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
    # Up-days with higher volume
    hist = []
    d = date(2025, 1, 2)
    prev_close = 100.0
    for i in range(10):
        close = prev_close + (1 if i % 2 == 0 else -0.5)
        vol = 20_000 if close > prev_close else 5_000
        hist.append(DailyOHLCV(
            ticker="T", trade_date=d + timedelta(days=i),
            open=prev_close, high=close+0.5, low=close-0.5, close=close, volume=vol
        ))
        prev_close = close
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_updown_volume(hist)
    assert pts == 8

def test_score_prior_advance():
    hist = _make_history(70, trending_up=True, base_close=100.0)
    # Close ~135 after 70 bars of +0.5 each → 135/100 = 1.35 > 1.15
    eng = AccumulationEngine(market="TSE")
    pts, flags = eng._score_prior_advance(hist)
    assert pts == 5
```

- [ ] **Step 2: Implement Dimension C methods**

```python
def _score_institutional_consec(self, proxy: TWSEChipProxy | None) -> tuple[int, list[str]]:
    if proxy is None or not proxy.is_available:
        return 0, []
    days = max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days)
    if days >= 5:
        return 20, [f"ACCUM_INST_CONSEC:{days}D"]
    if days >= 3:
        return 12, [f"ACCUM_INST_CONSEC:{days}D"]
    if days >= 1:
        return 5, [f"ACCUM_INST_CONSEC:{days}D"]
    return 0, []

def _score_institutional_net_trend(self, proxy: TWSEChipProxy | None) -> tuple[int, list[str]]:
    # Phase 4.20 proxy: uses consecutive days as proxy for 10d net trend
    # Full implementation (ChipProxyFetcher.fetch_history) deferred to Phase 4.21
    if proxy is None or not proxy.is_available:
        return 0, []
    if proxy.foreign_consecutive_buy_days >= 3:
        return 10, ["ACCUM_NET_TREND_PROXY:CONSEC3"]
    if proxy.foreign_consecutive_buy_days >= 1:
        return 5, ["ACCUM_NET_TREND_PROXY:CONSEC1"]
    return 0, []

def _score_updown_volume(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    last10 = sorted_h[-10:] if len(sorted_h) >= 10 else sorted_h
    if len(last10) < 4:
        return 0, []
    up_vols = [b.volume for i, b in enumerate(last10[1:], 1) if b.close > last10[i-1].close]
    dn_vols = [b.volume for i, b in enumerate(last10[1:], 1) if b.close < last10[i-1].close]
    if not up_vols or not dn_vols:
        return 0, []
    if sum(up_vols)/len(up_vols) > sum(dn_vols)/len(dn_vols):
        return 8, ["ACCUM_UP_VOL_DOMINATES"]
    return 0, []

def _score_market_relative_strength(
    self, history: list[DailyOHLCV], taiex_history: list[DailyOHLCV]
) -> tuple[int, list[str]]:
    if not taiex_history or len(history) < 5 or len(taiex_history) < 5:
        return 0, []
    sorted_s = sorted(history, key=lambda x: x.trade_date)[-5:]
    sorted_t = sorted(taiex_history, key=lambda x: x.trade_date)[-5:]
    protected = 0
    for i in range(1, min(len(sorted_s), len(sorted_t))):
        taiex_chg = (sorted_t[i].close / sorted_t[i-1].close - 1) if sorted_t[i-1].close > 0 else 0
        stock_chg = (sorted_s[i].close / sorted_s[i-1].close - 1) if sorted_s[i-1].close > 0 else 0
        if taiex_chg < 0 and stock_chg > taiex_chg / 2:
            protected += 1
    if protected >= 2:
        return 7, [f"ACCUM_MKT_PROTECT:{protected}"]
    return 0, []

def _score_proximity_to_resistance(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = [d.close for d in sorted_h]
    if len(closes) < 60:
        return 0, []
    high60 = max(closes[-60:])
    ratio = closes[-1] / high60 if high60 > 0 else 0
    if 0.95 <= ratio < 1.03:
        return 5, [f"ACCUM_VS_HIGH:{(ratio-1)*100:.1f}PCT"]
    return 0, [f"ACCUM_VS_HIGH:{(ratio-1)*100:.1f}PCT"]

def _score_prior_advance(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = [d.close for d in sorted_h]
    if len(closes) < 60:
        return 0, []
    min60 = min(closes[-60:])
    if min60 > 0 and closes[-1] / min60 >= 1.15:
        return 5, [f"ACCUM_PRIOR_ADVANCE:{(closes[-1]/min60-1)*100:.0f}PCT"]
    return 0, []
```

- [ ] **Step 3: Run Dimension C tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -k "inst_consec or updown or prior_advance or proximity" -v
```
Expected: all PASSED

- [ ] **Step 4: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py tests/unit/test_accumulation_engine.py
git commit -m "feat: AccumulationEngine Dimension C scoring (chip behavior factors)"
```

---

## Task 6: `score_full()` — Aggregate + Grade

**Files:**
- Modify: `src/taiwan_stock_agent/domain/accumulation_engine.py`
- Modify: `tests/unit/test_accumulation_engine.py`
- Create: `config/accumulation_params.json`

Wire all factors into a single `score_full()` method, normalize to 0–100, assign grade.

- [ ] **Step 1: Create params config**

```json
{
  "_comment": "Tunable parameters for AccumulationEngine (Phase 4.20)",
  "grade_thresholds": {
    "COIL_PRIME": 70,
    "COIL_MATURE": 50,
    "COIL_EARLY": 35
  },
  "raw_max_pts": 150
}
```
Save to `config/accumulation_params.json`.

- [ ] **Step 2: Write failing integration test**

```python
def test_score_full_prime_grade():
    """A well-constructed accumulation setup should return COIL_PRIME."""
    # 300 bars trending up then flat with institutional buying
    hist = _make_history(300, trending_up=True, base_close=50.0)
    # Force BB compression (flat last 30 bars)
    for bar in hist[-30:]:
        bar.high = 65.1
        bar.low = 64.9
        bar.close = 65.0
        bar.volume = 3_000  # volume dry-up
    proxy = _make_proxy(foreign_days=5)
    taiex = _make_history(30, trending_up=True, base_close=18000.0)
    eng = AccumulationEngine(market="TSE")
    result = eng.score_full(
        history=hist,
        proxy=proxy,
        taiex_regime="neutral",
        taiex_history=taiex,
        turnover_20ma=30_000_000,
    )
    assert result["grade"] in ("COIL_PRIME", "COIL_MATURE")
    assert result["score"] >= 50
    assert "flags" in result
    assert "score_breakdown" in result

def test_score_full_gate_fail_returns_none():
    hist = _make_history(80, trending_up=True, base_close=100.0)
    for bar in hist[-10:]:
        bar.close = 9999.0  # broken out
    eng = AccumulationEngine(market="TSE")
    result = eng.score_full(
        history=hist, proxy=None,
        taiex_regime="neutral", taiex_history=[],
        turnover_20ma=30_000_000,
    )
    assert result is None  # gate failed → not eligible

def test_score_full_grade_thresholds():
    # Scores map to correct grades
    eng = AccumulationEngine(market="TSE")
    assert eng._grade(72) == "COIL_PRIME"
    assert eng._grade(55) == "COIL_MATURE"
    assert eng._grade(40) == "COIL_EARLY"
    assert eng._grade(20) is None
```

- [ ] **Step 3: Implement `score_full` and `_grade`**

```python
def _grade(self, score: int) -> str | None:
    thresholds = self._params.get("grade_thresholds", {
        "COIL_PRIME": 70, "COIL_MATURE": 50, "COIL_EARLY": 35
    })
    if score >= thresholds["COIL_PRIME"]:
        return "COIL_PRIME"
    if score >= thresholds["COIL_MATURE"]:
        return "COIL_MATURE"
    if score >= thresholds["COIL_EARLY"]:
        return "COIL_EARLY"
    return None

def score_full(
    self,
    history: list[DailyOHLCV],
    proxy: TWSEChipProxy | None,
    taiex_regime: str,
    taiex_history: list[DailyOHLCV],
    turnover_20ma: float,
) -> dict[str, Any] | None:
    """Returns grade dict or None if gate fails."""
    passed, gate_flags = self._gate_check(history, taiex_regime, turnover_20ma)
    if not passed:
        return None

    breakdown: dict[str, int] = {}
    all_flags: list[str] = gate_flags[:]
    raw = 0

    factors = [
        ("bb_compression", self._score_bb_compression(history)),
        ("volume_dryup", self._score_volume_dryup(history)),
        ("consolidation_range", self._score_consolidation_range(history)),
        ("atr_contraction", self._score_atr_contraction(history)),
        ("inside_bars", self._score_inside_bars(history)),
        ("ma_convergence", self._score_ma_convergence(history)),
        ("obv_trend", self._score_obv_trend(history)),
        ("kd_low_flat", self._score_kd_low_flat(history)),
        ("close_above_midline", self._score_close_above_midline(history)),
        ("inst_consec", self._score_institutional_consec(proxy)),
        ("inst_net_trend", self._score_institutional_net_trend(proxy)),
        ("updown_volume", self._score_updown_volume(history)),
        ("market_strength", self._score_market_relative_strength(history, taiex_history)),
        ("proximity_resistance", self._score_proximity_to_resistance(history)),
        ("prior_advance", self._score_prior_advance(history)),
    ]

    for name, (pts, flags) in factors:
        breakdown[name] = pts
        raw += pts
        all_flags.extend(flags)

    raw_max = self._params.get("raw_max_pts", 150)
    score = min(100, round(raw / raw_max * 100))
    grade = self._grade(score)

    if grade is None:
        return None

    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = [d.close for d in sorted_h]
    high60 = max(closes[-60:]) if len(closes) >= 60 else closes[-1]
    vols = [d.volume for d in sorted_h]
    avg20v = sum(vols[-20:]) / 20 if len(vols) >= 20 else 0
    avg5v = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
    vol_ratio = avg5v / avg20v if avg20v > 0 else 1.0

    return {
        "grade": grade,
        "score": score,
        "raw_pts": raw,
        "flags": all_flags,
        "score_breakdown": breakdown,
        "bb_pct": next((float(f.split(":")[1]) for f in all_flags if f.startswith("ACCUM_BB_PCT:")), None),
        "vol_ratio": round(vol_ratio, 2),
        "inst_consec_days": max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days) if proxy and proxy.is_available else 0,
        "vs_60d_high_pct": round((closes[-1] / high60 - 1) * 100, 2) if high60 > 0 else 0.0,
        # Extract consol_range_pct from flags (required CSV column)
        "consol_range_pct": next(
            (float(f.split(":")[1].replace("PCT", "")) for f in all_flags if f.startswith("ACCUM_RANGE:")),
            None
        ),
    }
```

- [ ] **Step 4: Run all engine tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -v
```
Expected: all PASSED (no failures)

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```
Expected: all existing tests still PASS

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py config/accumulation_params.json tests/unit/test_accumulation_engine.py
git commit -m "feat: AccumulationEngine score_full() + grade thresholds + params config"
```

---

## Task 7: `coil_scan.py` — Standalone Scanner

**Files:**
- Create: `scripts/coil_scan.py`

Standalone script that loads tickers, runs AccumulationEngine, saves `coil_YYYY-MM-DD.csv`, and renders a Rich table. Mirrors `batch_plan.py` structure but simpler (no LLM, no DB write).

- [ ] **Step 1: Create `coil_scan.py`**

```python
"""Accumulation scanner — runs AccumulationEngine on multiple tickers.

Usage:
    python scripts/coil_scan.py                          # 互動式產業選擇
    python scripts/coil_scan.py --sectors 1 4
    python scripts/coil_scan.py --save-csv
    python scripts/coil_scan.py --date 2026-04-13
"""
from __future__ import annotations

import argparse, csv, json, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TaskProgressColumn
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.domain.accumulation_engine import AccumulationEngine
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

_console = Console()
_lock = Lock()

COIL_CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "name", "market", "grade", "score",
    "bb_pct", "vol_ratio", "consol_range_pct", "inst_consec_days",
    "weeks_consolidating", "vs_60d_high_pct", "score_breakdown", "flags",
]

GRADE_COLOR = {
    "COIL_PRIME": "bold magenta",
    "COIL_MATURE": "bold cyan",
    "COIL_EARLY": "yellow",
}
```

The script reuses helpers from `batch_plan.py`. To avoid a circular import (Task 8 has `batch_plan.py` importing from `coil_scan.py`), use **local imports inside functions** — not at module level. The correct function names in `batch_plan.py` are:

```python
# Inside run_coil_scan() or main() — local scope import, NOT at module top:
def run_coil_scan(...):
    from batch_plan import (
        _build_industry_map,   # not _get_latest_industry_map
        _build_name_map,       # not _get_latest_name_map
        _build_market_map,     # not _get_latest_market_map
        _select_sectors,       # not _select_industries_interactive
        _default_date,         # not _get_analysis_date
    )
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
```

**Do NOT import `_notify_telegram` from `batch_plan`** — its signature (`csv_path, scan_date, top, min_confidence`) is designed for momentum results. Write a dedicated `_notify_coil_telegram(coil_csv_path, scan_date)` in `coil_scan.py` that reads the coil CSV and formats a separate Telegram message showing grade/score/vs前高 for COIL_MATURE+ stocks.

Implement `_scan_one_coil(ticker, analysis_date, finmind, chip_fetcher, market, taiex_cache)` that:
1. Calls `finmind.fetch_ohlcv(ticker, ...)` for 300 trading days
2. Calls `chip_fetcher.fetch(ticker, analysis_date)`
3. Calls `AccumulationEngine(market=market).score_full(...)`
4. Returns a result dict or `None`

Implement `run_coil_scan(tickers, analysis_date, workers, market_map, name_map, csv_path, notify)` that:
1. Creates shared `FinMindClient` + `ChipProxyFetcher`
2. Fetches TAIEX history once (shared across workers)
3. ThreadPoolExecutor with progress bar
4. Filters out `None` results (gate failed)
5. Sorts by score descending
6. Saves CSV if `csv_path` provided
7. Prints Rich table
8. Calls `_notify_telegram` if `notify=True` with COIL_MATURE+ stocks

Implement `_save_coil_csv(results, analysis_date, csv_path)` mirroring `_save_csv` in `batch_plan.py`.

Implement `_print_coil_table(results, scan_date, name_map)` using Rich table with columns: Rank / Ticker / 名稱 / 等級 / 分數 / BB壓縮 / 法人連買 / 橫盤週 / vs前高.

Implement `_weeks_consolidating(history)`: count consecutive sessions from end where close stays within current 20d high/low spread, divide by 5.

- [ ] **Step 1b: Write unit tests for `_weeks_consolidating` and `consol_range_pct`**

```python
# Add to tests/unit/test_accumulation_engine.py

from coil_scan import _weeks_consolidating  # local import after sys.path setup

def test_weeks_consolidating_flat():
    hist = _make_history(30, flat=True, base_close=100.0)
    # All closes flat within narrow range → many consecutive sessions
    weeks = _weeks_consolidating(hist)
    assert weeks >= 4  # 30 flat sessions → at least 6 weeks

def test_weeks_consolidating_volatile_breaks_early():
    hist = _make_history(30, flat=True, base_close=100.0)
    # Inject a spike 5 sessions ago to break the streak
    hist[-5].close = 115.0  # outside 20d spread
    weeks = _weeks_consolidating(hist)
    assert weeks <= 1  # streak broken by spike

def test_score_full_returns_consol_range_pct():
    hist = _make_history(300, trending_up=True, base_close=50.0)
    for bar in hist[-30:]:
        bar.high = 65.1; bar.low = 64.9; bar.close = 65.0; bar.volume = 3_000
    proxy = _make_proxy(foreign_days=5)
    eng = AccumulationEngine(market="TSE")
    result = eng.score_full(hist, proxy, "neutral", [], 30_000_000)
    if result is not None:
        assert "consol_range_pct" in result
        # 20d spread = (65.1 - 64.9) / 64.9 ≈ 0.3% → much < 5% → 15pts triggered
        assert result["consol_range_pct"] is not None
```

Run these tests after implementing `_weeks_consolidating` in `coil_scan.py`:
```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -k "weeks_consolidating or consol_range" -v
```
Expected: all PASSED

- [ ] **Step 2: Test coil_scan manually with a few tickers (smoke test)**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/python scripts/coil_scan.py --tickers 2330 2454 3008 --no-save
```
Expected: Rich table appears, no crash, results show COIL_* grades or empty table if no stocks qualify.

- [ ] **Step 3: Test CSV save**

```bash
.venv/bin/python scripts/coil_scan.py --tickers 2330 2454 3008 --save-csv --date 2026-04-13
```
Expected: `data/scans/coil_2026-04-13.csv` created with correct headers.

- [ ] **Step 4: Commit**

```bash
git add scripts/coil_scan.py
git commit -m "feat: coil_scan.py standalone accumulation scanner with Rich table + CSV output"
```

---

## Task 8: Integrate Pass 2 into `batch_plan.py`

**Files:**
- Modify: `scripts/batch_plan.py` (~20 lines added)

After the existing `_print_table` + `_save_csv` calls in `run_batch`, add a Pass 2 that calls `coil_scan.run_coil_scan` on the same ticker list, reusing the already-warmed `FinMindClient` cache.

- [ ] **Step 1: No module-level import needed**

Do NOT add a module-level import of `coil_scan` at the top of `batch_plan.py` — that creates a circular import (`coil_scan` imports from `batch_plan`). Instead use a **local import inside `run_batch()`** at the point of call:

- [ ] **Step 2: Add Pass 2 call at end of `run_batch()` function**

Find the block in `run_batch()` (around line 953–956):
```python
    _print_table(results, top, min_confidence, ...)
    if csv_path:
        _save_csv(results, analysis_date, csv_path, sort_by=sort_by)
```

Add after it:
```python
    # --- Pass 2: Accumulation scan (independent engine, local import avoids circular dep) ---
    try:
        from coil_scan import run_coil_scan as _run_coil_scan
        _console.rule("[bold magenta]蓄積雷達 Pass 2[/bold magenta]")
        coil_csv = None
        if csv_path:
            coil_csv = csv_path.parent / f"coil_{analysis_date.isoformat()}.csv"
        _run_coil_scan(
            tickers=tickers,
            analysis_date=analysis_date,
            workers=workers,
            market_map=market_map,
            name_map=name_map,
            csv_path=coil_csv,
            notify=notify,
        )
    except ImportError:
        _console.print("  [dim yellow]⚠ coil_scan not available, skipping Pass 2[/dim yellow]")
```

- [ ] **Step 3: Smoke test `make plan` end-to-end with a small ticker set**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/python scripts/batch_plan.py --tickers 2330 2454 3008 --no-llm --save-csv
```
Expected: 
- Existing momentum table renders as before
- "蓄積雷達 Pass 2" separator appears
- Accumulation table renders (or shows "no results" if none qualify)
- Two CSVs created: `scan_*.csv` and `coil_*.csv`

- [ ] **Step 4: Run test suite**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: batch_plan Pass 2 — accumulation scan runs after momentum scan"
```

---

## Task 9: `make bot` — 蓄積雷達 Display Block

**Files:**
- Modify: `scripts/bot.py`

Add a small "蓄積雷達" section at the bottom of the Bot Status panel, reading from the latest `coil_*.csv` file. Mirrors the existing Watchlist Prices block pattern.

- [ ] **Step 1: Add `_load_latest_coil_csv()` helper to `bot.py`**

```python
def _load_latest_coil_csv() -> list[dict]:
    """Load top rows from latest coil_*.csv. Returns [] if not found."""
    scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
    coil_files = sorted(scan_dir.glob("coil_*.csv"), reverse=True)
    if not coil_files:
        return []
    try:
        with coil_files[0].open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))[:5]  # top 5 only
    except Exception:
        return []
```

- [ ] **Step 2: Add coil table render to `_render_status_panel()`**

In the existing `_render_status_panel()` function, after the Watchlist Prices table block, add:

```python
    # --- 蓄積雷達 ---
    coil_rows = _load_latest_coil_csv()
    if coil_rows:
        coil_tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta",
                         show_edge=False, pad_edge=False)
        coil_tbl.add_column("代號", width=8)
        coil_tbl.add_column("等級", width=12)
        coil_tbl.add_column("分數", justify="right", width=6)
        coil_tbl.add_column("vs前高", justify="right", width=8)
        grade_style = {"COIL_PRIME": "bold magenta", "COIL_MATURE": "cyan", "COIL_EARLY": "yellow"}
        for row in coil_rows:
            grade = row.get("grade", "")
            style = grade_style.get(grade, "white")
            coil_tbl.add_row(
                f"[{style}]{row.get('ticker', '')}[/{style}]",
                f"[{style}]{grade}[/{style}]",
                row.get("score", "--"),
                row.get("vs_60d_high_pct", "--"),
            )
        status_content = Group(existing_content, Rule(style="dim magenta"), Text("蓄積雷達", style="bold magenta"), coil_tbl)
    else:
        status_content = existing_content
```

(Adapt to the actual variable names in `_render_status_panel()` — the pattern mirrors the existing Watchlist Prices addition.)

- [ ] **Step 3: Smoke test `make bot`**

```bash
.venv/bin/python scripts/bot.py
```
Expected: Bot Status panel shows "蓄積雷達" section at bottom. If no `coil_*.csv` exists, section is hidden (no crash).

- [ ] **Step 4: Commit**

```bash
git add scripts/bot.py
git commit -m "feat: bot.py 蓄積雷達 block in Bot Status panel (reads latest coil CSV)"
```

---

## Task 10: Makefile Targets

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Add targets**

Add after existing `backtest` target:

```makefile
# ── 蓄積雷達掃描 ──────────────────────────────────────────────────────────────
coil:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/coil_scan.py --save-csv $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS))
else
	$(PYTHON) scripts/coil_scan.py --save-csv --date $(DATE) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS))
endif

# ── 蓄積信號回測 ──────────────────────────────────────────────────────────────
coil-backtest:
	$(PYTHON) scripts/coil_backtest.py \
		$(if $(DATE_FROM),--date-from $(DATE_FROM)) \
		$(if $(DATE_TO),--date-to $(DATE_TO))

# ── 蓄積因子分析 ──────────────────────────────────────────────────────────────
coil-factor-report:
	$(PYTHON) scripts/coil_factor_report.py

# ── 蓄積引擎參數優化 ──────────────────────────────────────────────────────────
optimize-coil:
	$(PYTHON) scripts/optimize_coil.py
```

Also add `coil coil-backtest coil-factor-report optimize-coil` to the `.PHONY` line at top.

- [ ] **Step 2: Verify targets work**

```bash
make coil TICKERS="2330 2454" DATE=2026-04-13
```
Expected: runs coil_scan.py, saves CSV, exits cleanly.

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "feat: Makefile targets — coil, coil-backtest, coil-factor-report, optimize-coil"
```

---

## Task 11: `coil_backtest.py` — Historical Replay

**Files:**
- Create: `scripts/coil_backtest.py`

Replay AccumulationEngine over historical data. Success criterion: close breaks 20d-high within T+10 OR T+10 return ≥ 5%.

- [ ] **Step 1: Create `coil_backtest.py`**

Script structure:
1. CLI args: `--date-from`, `--date-to`, `--tickers` (optional subset), `--workers`
2. For each trading day in range:
   - Run AccumulationEngine on each ticker using data up to that day
   - Record signals (grade, score, entry_close)
3. For each signal: look up T+10 close, compute success (criterion C)
4. Report: per-grade win rate, avg return, avg days-to-breakout, sample size
5. Rich table output + optional CSV

Key implementation note: Use `FinMindClient.fetch_ohlcv(ticker, end_date=signal_date, n_days=310)` to simulate point-in-time data (no lookahead bias). For T+10 outcome, fetch a second window ending at `signal_date + 14 calendar days`.

```python
def _check_success(entry_close: float, future_bars: list[DailyOHLCV],
                   entry_20d_high: float) -> tuple[bool, int, float]:
    """Returns (success, days_to_event, final_return)."""
    sorted_bars = sorted(future_bars, key=lambda x: x.trade_date)[:10]
    for i, bar in enumerate(sorted_bars, 1):
        if bar.close >= entry_20d_high:  # criterion A: breaks 20d high
            return True, i, (bar.close / entry_close - 1)
    if sorted_bars:
        final_ret = sorted_bars[-1].close / entry_close - 1
        if final_ret >= 0.05:  # criterion B: +5% in 10 days
            return True, len(sorted_bars), final_ret
        return False, len(sorted_bars), final_ret
    return False, 0, 0.0
```

- [ ] **Step 2: Smoke test with small date range**

```bash
.venv/bin/python scripts/coil_backtest.py --date-from 2026-01-01 --date-to 2026-02-28 --tickers 2330 2454 3008 2382
```
Expected: runs without crash, outputs per-grade win rate table (may show "no signals" if period too short).

- [ ] **Step 3: Commit**

```bash
git add scripts/coil_backtest.py
git commit -m "feat: coil_backtest.py — accumulation signal historical replay with criterion C"
```

---

## Task 12: `coil_factor_report.py` + `optimize_coil.py`

**Files:**
- Create: `scripts/coil_factor_report.py`
- Create: `scripts/optimize_coil.py`

- [ ] **Step 1: Create `coil_factor_report.py`**

Uses backtest results (from `coil_backtest.py` with `--save-results` flag) to compute per-factor lift:
1. For each of 15 factors: split results into "factor present" (pts > 0) vs "factor absent" (pts = 0)
2. Compute win rate for each group
3. Lift = win_rate_present / win_rate_absent
4. Rich table sorted by lift descending
5. Flag factors with lift < 1.05 as "⚠ WEAK"

Requires `score_breakdown` JSON from the coil CSV (written in Task 7).

- [ ] **Step 2: Create `optimize_coil.py`**

Grid search over `accumulation_params.json` tunable values:
- `grade_thresholds.COIL_PRIME`: range 60–80 step 5
- `grade_thresholds.COIL_MATURE`: range 40–60 step 5

Walk-forward: train on `date_from` to `date_to - 60d`, test on last 60 days.

Best params written to `config/accumulation_params.json` after interactive `make tune-review` confirmation (same pattern as `scripts/apply_tuning.py`).

- [ ] **Step 3: Smoke test**

```bash
.venv/bin/python scripts/coil_factor_report.py --help
.venv/bin/python scripts/optimize_coil.py --help
```
Expected: both scripts show help without crash.

- [ ] **Step 4: Run full test suite one final time**

```bash
.venv/bin/pytest tests/unit/ -v --tb=short
```
Expected: all tests pass (224+ passing, accumulation tests added)

- [ ] **Step 5: Final commit**

```bash
git add scripts/coil_factor_report.py scripts/optimize_coil.py
git commit -m "feat: coil_factor_report.py + optimize_coil.py — factor lift analysis and param optimization"
```

---

## Task 13: Update Docs + Push

- [ ] **Step 1: Update CLAUDE.md Phase gates**

Add Phase 4.20 entry:
```
| Phase 4.20 | ✅ Done | AccumulationEngine (15 factors/3 dims) ✅ · coil_scan.py ✅ · Pass 2 in batch_plan ✅ · bot 蓄積雷達 block ✅ · coil-backtest/factor-report/optimize-coil ✅ |
```

- [ ] **Step 2: Update README.md**

Add `make coil` to the daily workflow table and Makefile commands section.

- [ ] **Step 3: Commit + push**

```bash
git add CLAUDE.md README.md
git commit -m "docs: Phase 4.20 accumulation engine — update CLAUDE.md gate + README"
git push
```

---

## Checkpoint: After Task 6

After Task 6 (`score_full` complete), pause and run a real-world smoke test before integrating into the scan pipeline:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from taiwan_stock_agent.domain.accumulation_engine import AccumulationEngine
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
from datetime import date

fm = FinMindClient()
cp = ChipProxyFetcher()
hist = fm.fetch_ohlcv('2330', date(2026, 4, 15), n_days=310)
proxy = cp.fetch('2330', date(2026, 4, 15))
eng = AccumulationEngine(market='TSE')
result = eng.score_full(hist, proxy, taiex_regime='neutral', taiex_history=[], turnover_20ma=5e9)
print(result)
"
```

If result is `None`, 2330 didn't pass the gate (it's likely not in accumulation — that's correct). Try with a stock that's been flat recently.
