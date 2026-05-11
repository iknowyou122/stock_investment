# Dynamic BB Threshold + Momentum Walk Quality Confirmers — Design Spec

**Date:** 2026-05-11
**Status:** Approved
**Motivation:** 德律 (3030) post-mortem — 04-08/04-09 had proximity_pts=12, BB compressed to 17%, but failed G2's absolute ≤15% threshold. Two additional quality-confirmer factors (5MA walk, BB upper walk) are added to differentiate candidates that already pass the gates.

---

## Root Cause Summary

| Cause | Impact |
|-------|--------|
| G2 uses absolute BB width ≤15% | Stocks with structurally higher volatility (e.g. 3030) never reach 15% even when at historical lows |
| No short-term trend quality factor | 5MA walk behaviour (籌碼承接) is unscored |
| No coil-at-resistance factor | Repeatedly testing BB upper while compressed is the strongest pre-breakout signal; currently unscored |

---

## Design Principle

5MA walk and BB upper walk are **quality confirmers, not discovery signals**. They only carry meaning after a stock has already passed the Gate layer. Points are intentionally small (2–3 pts) — their role is to differentiate among already-qualified candidates, not to independently surface stocks.

---

## Fix 1: G2 Dynamic BB Threshold

**File:** `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` → `_check_gates()`

### Current behaviour
`bb_w <= 0.15` — absolute BB width gate.

### New behaviour
`bb_width_pct <= 35.0` — BB width must be in the bottom 35th percentile of the past 60 trading days for this ticker.

`bb_width_pct` is already computed by `_calculate_bb()` (returns 4-tuple; 4th element is the percentile rank). G2 currently discards it (`_`). The fix captures it and uses it as the threshold.

### Flag format change
| State | Old flag | New flag |
|-------|----------|----------|
| Pass | `GATE_PASS:G2_BB:12.0%` | `GATE_PASS:G2_BB_PCT:32.5p` |
| Fail | `GATE_FAIL:G2_BB_WIDE:45.0%` | `GATE_FAIL:G2_BB_WIDE_PCT:55.2p` |
| No data | `GATE_SKIP:G2_NO_BB` | unchanged |

### Retrospective validation
3030 德律 on 04-08: bb_width_pct = 32.5 → PASS (≤35). On 04-09: 34.1 → PASS. Breakout occurred 04-10.

### Constraints
- Requires ≥ 60 days of OHLCV history for percentile computation. If fewer days available, fall back to absolute ≤15% gate (existing behaviour).
- No change to other gates.

---

## Fix 2: 5MA Walk Factor

**Files:**
- `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` → new `_ma5_walk_score()` + `_ScoreBreakdown.ma5_walk_pts`
- `scripts/surge_scan.py` → new `_ma5_walk_score()` helper (standalone, same logic)

### Detection logic
Look at the most recent 10 trading days (or all available if < 10). Count days where `close >= MA5`. If ratio ≥ 0.8 (e.g. 8/10 days) → walking.

```python
def _ma5_walk_score(history: list[DailyOHLCV], n: int = 10) -> int:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = pd.Series([d.close for d in sorted_h])
    if len(closes) < 5:
        return 0
    ma5 = closes.rolling(5).mean()
    window = min(n, len(closes))
    ratio = (closes.iloc[-window:] >= ma5.iloc[-window:]).mean()
    return 2 if ratio >= 0.8 else 0
```

### In pre-breakout scan (Pillar 1, +2 pts)
- Added to `_ScoreBreakdown` as `ma5_walk_pts: int = 0`
- Computed in `_score_pillar1()` and included in Pillar 1 sum
- Pillar 1 cap unchanged (39); existing pts budget absorbs +2 since most stocks won't score full Pillar 1
- Flag: `MA5_WALK` added to flags when pts > 0

### In surge scan
- +2 to surge score when walking 5MA after surge (surge_day ≤ 2)
- −1 when close broke below MA5 on the surge day or following day
- No flag for surge (score-only)

---

## Fix 3: BB Upper Walk Factor

**Files:**
- `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` → new `_bb_upper_walk_score()` + `_ScoreBreakdown.bb_upper_walk_pts`
- `scripts/surge_scan.py` → new `_bb_upper_walk_score()` helper

### Detection logic
Look at the most recent 5 trading days. Count days where `close >= bb_upper × 0.97` (within 3% of upper band). Require BB upper itself to have a positive 5-day slope (rising, not flat).

```python
def _bb_upper_walk_score(history: list[DailyOHLCV], n: int = 5, tolerance: float = 0.03) -> int:
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = pd.Series([d.close for d in sorted_h])
    if len(closes) < 20:
        return 0
    ma = closes.rolling(20).mean()
    std = closes.rolling(20).std(ddof=0)
    bb_upper = ma + 2 * std
    window_upper = bb_upper.iloc[-n:]
    window_close = closes.iloc[-n:]
    near_upper = (window_close >= window_upper * (1 - tolerance)).sum()
    bb_upper_rising = (bb_upper.iloc[-1] > bb_upper.iloc[-n]) if len(bb_upper) >= n else False
    return 3 if (near_upper >= 3 and bb_upper_rising) else 0
```

### In pre-breakout scan (Pillar 3, conditional)
- Returns 3 pts only when BOTH:
  1. `proximity_pts == 12` (stock is in 92-99% zone)
  2. G2 passed (BB is compressed, stock is coiling)
- Returns 0 in all other cases
- Added to `_ScoreBreakdown` as `bb_upper_walk_pts: int = 0`
- Flag: `BB_UPPER_COIL` added when pts > 0
- Meaning: BB is compressing AND close repeatedly tests the upper band — the strongest pre-breakout coil pattern

### In surge scan
- Surge day ≤ 2 + BB upper walk → add tag `MOMENTUM_WALK` to output (informational, no score change)
- Surge day ≥ 3 + BB upper walk → −3 to surge score (exhaustion confirmation)

---

## Call order / integration

### `_check_gates()` change (Fix 1)
Replace:
```python
_, _, bb_w, _ = self._calculate_bb(ohlcv_history)
if bb_w is not None:
    if bb_w <= 0.15:
```
With:
```python
_, _, bb_w, bb_width_pct = self._calculate_bb(ohlcv_history)
if bb_w is not None:
    threshold_met = (bb_width_pct is not None and bb_width_pct <= 35.0) or (bb_width_pct is None and bb_w <= 0.15)
    if threshold_met:
```

### `_score_pillar1()` change (Fix 2)
Add at end:
```python
bd.ma5_walk_pts = self._ma5_walk_score(history)
```

### `_score_pillar3()` change (Fix 3)
Add conditionally:
```python
if bd.proximity_pts == 12 and g2_passed:
    bd.bb_upper_walk_pts = self._bb_upper_walk_score(history)
```
`g2_passed` is determined from gate results already computed before pillar scoring runs.

---

## Expected Impact (3030 retrospective)

| Fix | Effect on 04-08 |
|-----|-----------------|
| Fix 1 (G2 dynamic) | G2 PASS (was FAIL) → stock enters scoring |
| Fix 2 (5MA walk) | +2 pts if walking (needs to verify against history) |
| Fix 3 (BB upper walk) | +3 pts if close near BB upper (proximity=12 + G2 pass) |

---

## Test Requirements

- `test_g2_dynamic_threshold`: percentile ≤35 passes; percentile 36 fails; fallback to absolute when <60 days history
- `test_ma5_walk_score`: 8/10 days above MA5 → 2 pts; 7/10 → 0 pts; <5 days history → 0 pts
- `test_bb_upper_walk_score`: 3 of 5 days near upper + rising → pts > 0; <3 days → 0; not rising → 0
- `test_bb_upper_walk_conditional`: only awards pts when proximity=12 AND g2_passed
- Surge scan tests: walking 5MA post-surge adds score; surge day ≥3 + BB walk deducts
- Existing tests must remain green

---

## Out of Scope

- Changing Gate conditions other than G2
- Modifying Pillar 2 (chip data)
- Any UI or bot changes
- Backfilling historical scan CSVs
