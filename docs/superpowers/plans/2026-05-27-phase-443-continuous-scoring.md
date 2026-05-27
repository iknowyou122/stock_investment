# Phase 4.43 Continuous Scoring Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate "same-score clustering" in `make plan` output. Currently 67 factors all use integer bucket scoring (e.g. RSI 40–65 → +4 flat), so totals collapse onto a handful of integers (`72`, `75`, `78` …) and users cannot prioritize within a tier.

**Strategy:** Convert `_ScoreBreakdown` from `int` → `float`, then refactor the 8 highest-variance factors from bucket → continuous functions. The remaining 59 small-bucket factors stay integer for now; their +2/+3 noise won't cause collisions once the high-variance factors output decimals.

**Non-goals:**
- NOT touching SurgeRadar / Pullback / Early-Accum detectors (separate phase)
- NOT changing factor weights or pillar caps
- NOT changing the LONG/WATCH/CAUTION action thresholds

**Tech:** Python 3.10+, existing `_ScoreBreakdown` dataclass, `pytest.approx` for float-tolerant tests.

---

## File Map

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | **Modify** — `_ScoreBreakdown` int→float, 8 scoring helpers continuous, `total` returns float |
| `scripts/batch_plan.py` | **Modify** — display `confidence` as `f"{conf:.1f}"` in table/HTML, sort still works |
| `scripts/analyze.py` | **Modify** — single-stock display format |
| `src/taiwan_stock_agent/infrastructure/signal_recorder.py` | **Verify** — confidence column accepts float (already DB numeric) |
| `tests/unit/test_triple_confirmation_engine*.py` (multiple files) | **Migrate** — change `== N` assertions to `== pytest.approx(N, abs=0.5)` for converted factors |
| `tests/unit/test_*.py` (downstream) | **Verify** — no regressions |
| `CLAUDE.md` | **Modify** — add Phase 4.43 entry |

---

## Task 1: Convert `_ScoreBreakdown` to float

**Goal:** All `_pts` fields and the `total` property return float. Downstream code (`confidence`, sort, HTML) tolerates float.

- [ ] **Step 1: Change all `_pts` field types from `int` → `float`**

  In `triple_confirmation_engine.py` `_ScoreBreakdown` dataclass (lines ~218–304):
  - Change every `<name>_pts: int = 0` → `<name>_pts: float = 0.0`
  - Change every `<risk>: int = 0` → `<risk>: float = 0.0`
  - Keep `flags: list[str]` unchanged.

- [ ] **Step 2: Update `total` property return type**

  - Change signature: `def total(self) -> int:` → `def total(self) -> float:`
  - Inside `total`, do NOT cast to int. Let arithmetic propagate float.
  - Same for any helper properties: `momentum_pts`, `chip_pts`, `structure_pts` → `float`.

- [ ] **Step 3: Verify `total` callers tolerate float**

  ```bash
  grep -rn "\.total\b" src/taiwan_stock_agent/domain/triple_confirmation_engine.py | head -20
  grep -rn "confidence.*int\|int(.*confidence" src/ scripts/ | head -10
  ```
  - If any caller does `int(confidence)` explicitly, leave the int cast — that's display/storage rounding which is fine.
  - If any caller does `if confidence == 75:` (exact int compare), flag it.

- [ ] **Verification:**
  ```bash
  .venv/bin/python -c "from taiwan_stock_agent.domain.triple_confirmation_engine import _ScoreBreakdown; bd = _ScoreBreakdown(); bd.rsi_momentum_pts = 3.7; bd.volume_ratio_pts = 5.2; print(type(bd.total), bd.total)"
  # Expect: <class 'float'> 8.9
  ```

---

## Task 2: Convert 8 high-variance factors to continuous

These 8 produce ~70% of the score variance. After conversion, two cards with identical bucketed factors will almost always differ on these.

### 2.1 `_volume_ratio_score` (Pillar 1) — bucket 0/4/5/8 → continuous 0–8

Current (lines 937–952):
```python
if ratio >= 3.0: return 5, "VOL_EXHAUSTION_RISK"
if ratio >= 2.0: return 8, None
if ratio >= 1.2: return 4, None
return 0, None
```

**Replace with:**
```python
import math
if vol_20ma is None or vol_20ma == 0:
    return 0.0, None
ratio = ohlcv.volume / vol_20ma
if ratio < 1.0:
    return 0.0, None
# Log curve: 1x→0, 2x→6.0, 3x→8.0 (peak), 4x+→fade with risk flag
if ratio >= 3.0:
    # exhaustion: linear decay 8.0 → 4.0 at 6x
    pts = max(4.0, 8.0 - (ratio - 3.0) * 1.0)
    return round(pts, 2), "VOL_EXHAUSTION_RISK"
# 1x–3x: log-shaped 0 → 8
pts = math.log(ratio) / math.log(3.0) * 8.0
return round(pts, 2), None
```

- [ ] Update `_volume_ratio_score` per above.
- [ ] Change return type annotation to `tuple[float, str | None]`.
- [ ] Update test `test_triple_confirmation_engine_v2.py::test_volume_ratio_*` (likely 3–4 tests): `assert pts == 4` → `assert pts == pytest.approx(3.78, abs=0.1)` (or whatever the formula yields for that ratio).

### 2.2 `_close_strength_score` (Pillar 1) — bucket -2/0/2/4 → continuous -2–4

Current (lines 962–978):
```python
if ratio >= 0.8: return 4, None
if ratio >= 0.6: return 2, None
if ratio >= 0.4: return 0, None
return -2, "CLOSE_WEAK_OUT_PATTERN"
```

**Replace with:**
```python
bar_range = ohlcv.high - ohlcv.low
if bar_range <= 0:
    return 0.0, "DOJI_OR_HALT"
ratio = (ohlcv.close - ohlcv.low) / bar_range
# Linear: ratio 0.5 → 0, ratio 1.0 → 4, ratio 0.0 → -2
pts = (ratio - 0.5) * 8.0  # 0.5→0, 1.0→4.0, 0.0→-4.0
pts = max(-2.0, min(4.0, pts))  # clamp
flag = "CLOSE_WEAK_OUT_PATTERN" if ratio < 0.4 else None
return round(pts, 2), flag
```

- [ ] Update function + annotation.
- [ ] Tests: cs=0.85 used to be 4, now ~2.8. Need recalc — use `approx(2.8, abs=0.2)`.

### 2.3 `_rsi_momentum_score` (Pillar 1) — bucket 0/4 → continuous 0–4

Current (lines 1157–1170):
```python
return 4 if 40.0 <= rsi <= 65.0 else 0
```

**Replace with:**
```python
if rsi is None:
    return 0.0
# Peak at RSI 55, full points (4.0), taper to 0 outside 35–70
if 50.0 <= rsi <= 60.0:
    return 4.0
if 40.0 <= rsi < 50.0:
    return round((rsi - 40.0) / 10.0 * 4.0, 2)  # 40→0, 50→4
if 60.0 < rsi <= 70.0:
    return round((70.0 - rsi) / 10.0 * 4.0, 2)  # 60→4, 70→0
return 0.0
```

- [ ] Update function + annotation `-> float`.
- [ ] Test cases will need recalc.

### 2.4 `_volume_dryup_score` (Pillar 1) — bucket 0/4/8 → continuous 0–8

Current (lines 988–1003):
```python
if ratio < 0.60: return 8
if ratio < 0.80: return 4
return 0
```

**Replace with:**
```python
if avg_20d <= 0:
    return 0.0
ratio = avg_5d / avg_20d
# Lower ratio = drier = better. Map 0.4→8.0, 1.0→0.0, clamp.
if ratio >= 1.0:
    return 0.0
if ratio <= 0.4:
    return 8.0
pts = (1.0 - ratio) / 0.6 * 8.0
return round(pts, 2)
```

- [ ] Update + annotation.

### 2.5 `_proximity_score` (Pillar 3) — bucket 0/6/12 → continuous 0–12

Current (lines 1527–1536):
```python
if 0.92 <= ratio < 0.99: return 12
if 0.88 <= ratio < 0.92: return 6
return 0
```

**Replace with:**
```python
if twenty_day_high <= 0:
    return 0.0
ratio = close / twenty_day_high
if ratio >= 0.99 or ratio < 0.85:
    return 0.0  # too close (breakout already) or too far
# 0.85→0, 0.95→12, taper 0.95→0.99 back to 6
if ratio <= 0.95:
    pts = (ratio - 0.85) / 0.10 * 12.0
else:
    pts = 12.0 - (ratio - 0.95) / 0.04 * 6.0
return round(max(0.0, min(12.0, pts)), 2)
```

- [ ] Update + annotation.
- [ ] **IMPORTANT:** `proximity_pts == 12` is used as an integer trigger in two places (lines 850 + 2313). Change to `>= 11.5` (float-tolerant near-max check).

  ```bash
  grep -n "proximity_pts == 12\|proximity_pts == 6" src/
  ```
  Update both call sites.

### 2.6 `_bb_compression_score` (Pillar 3) — bucket 0/5/10 → continuous 0–10

Current (lines 1538–1549):
```python
if bb_width_raw < 0.08: return 10
if bb_width_raw < 0.12: return 5
return 0
```

**Replace with:**
```python
if bb_width_raw is None:
    return 0.0
# 0.04 (super tight) → 10, 0.16 (loose) → 0, linear
if bb_width_raw >= 0.16:
    return 0.0
if bb_width_raw <= 0.04:
    return 10.0
pts = (0.16 - bb_width_raw) / 0.12 * 10.0
return round(pts, 2)
```

- [ ] Update + annotation.

### 2.7 `cumul_flow_pts` assignment (Pillar 2B) — bucket 0/4/8 → continuous 0–8

Current inline (lines 1359–1368):
```python
if cumul_ratio >= 0.5: bd.cumul_flow_pts = 8
elif cumul_ratio >= 0.2: bd.cumul_flow_pts = 4
```

**Replace with:**
```python
cumul_net = proxy.cumul_foreign_20d + proxy.cumul_trust_20d
if avg_vol > 0 and cumul_net > 0:
    cumul_ratio = cumul_net / avg_vol
    if cumul_ratio >= 1.0:
        bd.cumul_flow_pts = 8.0
    elif cumul_ratio >= 0.1:
        # log map: 0.1→2.0, 0.5→6.0, 1.0→8.0
        import math
        bd.cumul_flow_pts = round(min(8.0, math.log(cumul_ratio / 0.1) / math.log(10.0) * 6.0 + 2.0), 2)
    else:
        bd.cumul_flow_pts = round(cumul_ratio / 0.1 * 2.0, 2)
    # Keep flags (HOT/WARM) based on threshold for HTML readability
    if cumul_ratio >= 0.5:
        bd.flags.append(f"CUMUL_FLOW_HOT:{cumul_ratio:.1f}x")
    elif cumul_ratio >= 0.2:
        bd.flags.append(f"CUMUL_FLOW_WARM:{cumul_ratio:.1f}x")
```

- [ ] Update inline block.

### 2.8 `_institution_strength_pts` helper (Pillar 2B) — used by foreign/trust/dealer

Currently `tiers=(0.0, 0.03, 0.08), points=(0, 4, 8, 12)` discrete buckets.

**Replace with continuous linear interpolation:**
```python
@staticmethod
def _institution_strength_pts(net_buy: float, avg_vol: float,
                              tiers: tuple, points: tuple) -> float:
    if avg_vol <= 0 or net_buy <= 0:
        return 0.0
    ratio = net_buy / avg_vol
    max_pts = float(points[-1])
    max_tier = tiers[-1]
    if ratio >= max_tier:
        # Cap at max, but allow slight overshoot up to +10%
        return round(min(max_pts * 1.0, max_pts), 2)
    # Linear from 0 → max_tier maps to 0 → max_pts
    return round(ratio / max_tier * max_pts, 2)
```

- [ ] Replace existing `_institution_strength_pts` method (search for `def _institution_strength_pts`).
- [ ] Verify all callers (foreign_strength, trust_strength, dealer_strength).

### 2.9 Pillar-cap enforcement

After all 8 are continuous, the per-pillar `min(_PILLAR1_MAX, ...)` caps in `total` still hold because they use `min()` on floats just fine. **No code change needed**, but verify by:

```bash
grep -n "_PILLAR1_MAX\|_PILLAR2_FREE_MAX\|_PILLAR3_MAX" src/taiwan_stock_agent/domain/triple_confirmation_engine.py
```

- [ ] Confirm `min()` calls work with float operands (Python: yes, no change).

---

## Task 3: Display formatting

- [ ] **Step 1: `scripts/batch_plan.py` table column**

  Find where `confidence` is rendered into Rich table cells (search for `r["confidence"]` near table builder). Wrap as `f"{r['confidence']:.1f}"`.

  ```bash
  grep -n 'r\["confidence"\]\|row.*confidence' scripts/batch_plan.py | head -20
  ```

- [ ] **Step 2: `scripts/batch_plan.py` HTML card**

  Find the card template `data-conf="{conf}"` (line ~2520). The HTML filter slider uses integer comparison — change attribute to integer rounded, but show decimal in visible text:

  ```python
  conf_int = int(round(r["confidence"]))   # for data-conf attribute (slider)
  conf_disp = f"{r['confidence']:.1f}"     # for visible display
  ```
  Update the template to use both.

- [ ] **Step 3: `scripts/analyze.py` single-stock display**

  Same treatment: 1-decimal display, integer for storage.

- [ ] **Step 4: Slider behavior**

  The HTML confidence slider in `filter-bar` uses `parseInt(c.dataset.conf, 10)`. Since `data-conf` is rounded int, no JS change needed. Verify slider still filters correctly after the change.

---

## Task 4: Test migration

The conversions break tests that assert exact integer values for the 8 factors. Strategy: tolerance-based assertions.

- [ ] **Step 1: Identify affected tests**

  ```bash
  grep -ln "volume_ratio_pts\|close_strength_pts\|rsi_momentum_pts\|proximity_pts\|bb_compression_pts\|cumul_flow_pts\|foreign_strength_pts\|trust_strength_pts\|dealer_strength_pts\|volume_dryup_pts" tests/unit/ | sort -u
  ```

  Expected: ~6–8 test files, ~40–60 assertions.

- [ ] **Step 2: Convert assertions per file**

  For each affected test file, change:
  ```python
  assert bd.volume_ratio_pts == 8
  ```
  to:
  ```python
  assert bd.volume_ratio_pts == pytest.approx(7.27, abs=0.3)   # log(2)/log(3)*8 ≈ 5.05  — recompute per test input!
  ```

  **DO NOT blindly find/replace.** Each test sets up specific input (volume, RSI, etc.) — manually compute the new expected value with the new formula. If unclear, run the test, observe the failure message, copy the actual value, verify it's reasonable, then assert.

- [ ] **Step 3: Tests that check `total` integer equality**

  ```bash
  grep -n "\.total ==\|bd\.total ==" tests/unit/ | head -30
  ```
  Convert to `pytest.approx(N, abs=1.0)` — broader tolerance since `total` aggregates many factors.

- [ ] **Step 4: Tests that check `confidence == N` in result dicts**

  ```bash
  grep -n "confidence.*==.*\d" tests/unit/ | head -30
  ```
  Same `pytest.approx(N, abs=1.0)` treatment.

- [ ] **Verification:**
  ```bash
  make test
  # Should see same number of tests passing (~653), with floats now.
  ```

---

## Task 5: Smoke-test on real data

- [ ] **Step 1: Run scan**

  ```bash
  .venv/bin/python scripts/batch_plan.py --tickers 2330 2454 3090 2308 --no-llm --date 2026-05-26
  ```

- [ ] **Step 2: Verify scores are now distinct**

  Open generated `data/scans/scan_2026-05-26.html`, inspect 5 cards in the same strategy tier. Confidence values should differ by ≥ 0.3 between any two.

  Acceptance: among any 10 LONG signals, no two have identical 1-decimal confidence.

- [ ] **Step 3: Verify slider/sort still work**

  - Drag slider → filtering still works.
  - Switch sort dropdown → re-orders correctly.
  - Click strategy pill → filters correctly.

---

## Task 6: Update CLAUDE.md

- [ ] Add Phase 4.43 row to the phase gates table:

  ```markdown
  | Phase 4.43 | ✅ Done | **連續評分（消除分數撞群）**：`_ScoreBreakdown` 全欄位 int→float ✅ · 8 個高方差因子改連續函數（volume_ratio/close_strength/rsi_momentum/volume_dryup/proximity/bb_compression/cumul_flow/institution_strength）✅ · HTML 顯示 1 位小數 ✅ · proximity 觸發邏輯改 `>= 11.5` ✅ · 約 50 個測試改用 `pytest.approx` ✅ · 653 unit tests passing ✅ |
  ```

---

## Rollback Plan

If continuous scoring causes regression in win-rate (verifiable via `make backtest`):

1. Revert `_ScoreBreakdown` to `int` fields
2. Revert 8 helper methods to bucketed versions
3. Revert test assertions

The change is mechanical and isolated — full git revert of this commit restores prior state.

---

---

# Phase 4.44 — TCE Remaining 59 Small-Bucket Factors

**Goal:** Convert all remaining `_score_*` methods in `triple_confirmation_engine.py` to continuous functions, completing the float migration started in Phase 4.43.

**Scope:** 59 factors across Pillars 1, 2A, 2B, 3, 4 plus 11 risk deductions. Most are +2/+3 buckets, so per-factor variance is small, but the cumulative effect on score uniqueness is meaningful.

---

## File Map

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | **Modify** — 59 helper methods + inline assignments |
| `tests/unit/test_triple_confirmation_engine*.py` | **Migrate** — ~150 additional assertions to `pytest.approx` |

---

## Task Groups (by Pillar)

### Task 4.44.1: Pillar 1 remaining factors (4 factors)

- [ ] `_price_direction_score` — currently 0/3 → keep as 0/3 (binary direction; no meaningful continuum)
- [ ] `_vwap_advantage_pts` — currently 0/6 → continuous (close-vwap)/vwap normalized
- [ ] `_trend_continuity_pts` — currently 0/3/5 → continuous based on % up days in last 10
- [ ] `_volume_escalation_pts` — currently 0/3/5 → continuous on slope of vol(t-3..t-1)
- [ ] `_volume_climax_pts` — currently 0/4 → continuous: (prior_spike_strength × current_dryup_strength)
- [ ] `_ma5_walk_pts` — currently 0/2 → continuous: ratio of close ≥ MA5 (0.0–2.0)
- [ ] `_dmi_initiation_pts` — currently 0/2/4/6 → continuous on ADX magnitude + DI cross freshness

**Note:** Some factors (like `_price_direction_score`) are genuinely binary signals — leave as-is. Only convert factors with an underlying continuous quantity.

### Task 4.44.2: Pillar 2A paid chip (5 factors)

- [ ] `_breadth_pts` — 0/5/10 → linear on `net_buyer_count_diff` from 0 to threshold
- [ ] `_concentration_pts` — 0/5/10 → linear on top-N broker share
- [ ] `_continuity_pts` — 0/3/5/8 → linear on broker-consensus days
- [ ] `_daytrade_filter_pts` — 0/7 → keep binary (regulatory flag)
- [ ] `_foreign_broker_pts` — 0/3/5 → linear on foreign-broker concentration

### Task 4.44.3: Pillar 2B free chip (20 factors)

These are mostly already covered by `_institution_strength_pts` (Phase 4.43 Task 2.8). Remaining:

- [ ] `institution_continuity_pts` (0–8 buckets per inst) — linear on continuous_buy_days
- [ ] `institution_consensus_pts` (0/4) — keep binary or soft via partial-credit on medium_count
- [ ] `margin_structure_pts` (-4 to +8) — continuous (margin_delta × price_direction)
- [ ] `margin_utilization_pts` (-4/0/+4) — continuous on utilization rate
- [ ] `sbl_pressure_pts` (0/-4/-8) — continuous on SBL ratio
- [ ] `consistent_accum_pts` (0/6) — linear on (buy_days / window)
- [ ] `inst_synergy_pts` (0/5/11) — continuous on inst_buy_pct
- [ ] `margin_declining_pts` (0/3) — keep binary (boolean flag)
- [ ] `ownership_concentration_pts` (-10/0/8) — continuous on (large_chg − retail_chg)
- [ ] `obv_accumulation_pts` (-3/0/2/3/5) — continuous on normalized OBV slope
- [ ] `vol_asymmetry_pts` (-4/-2/0/2/4) — continuous on (avg_up / avg_down)
- [ ] `dual_inst_flow_pts` (0/3/5) — linear on min(foreign_cumul, trust_cumul)
- [ ] `chip_cleanliness_pts` (0/4/7/10) — partial-credit K-of-6 (each signal weighted)
- [ ] `super_large_pts` (-4/0/+4/+8) — continuous on holdings_pct change
- [ ] `turnover_pts` (-3 to +4) — continuous on turnover rate distance from sweet spot
- [ ] `foreign_trend_pts` (-2 to +4) — continuous on W1/W2 acceleration ratio
- [ ] `short_cover_pts` (0 to +4) — continuous on short-cover rate
- [ ] `large_2w_trend_pts` (-3 to +5) — continuous on 2-week holdings slope
- [ ] `inst_accel_3d_pts` (-2 to +4) — continuous on (3d / 10d) ratio
- [ ] Phase 4.32 stealth factors (`obv_stealth_pts`, `margin_persist_decline_pts`, etc.) — continuous where underlying is continuous

### Task 4.44.4: Pillar 3 structure (10 factors)

- [ ] `ma_convergence_pts` — 0/4/8 → continuous on (1 - spread/0.04)
- [ ] `consolidation_weeks_pts` — 0/3/6 → continuous on consecutive_days/14
- [ ] `inside_bar_streak_pts` — 0–5 → already continuous-ish, ensure float
- [ ] `prior_advance_pts` — 0/2/5 → linear on advance %
- [ ] `ma_alignment_pts` — 0/5 → partial credit (1.67 per MA pair)
- [ ] `ma20_slope_pts` — 0/5 → linear on slope magnitude
- [ ] `relative_strength_pts` — 0/3/5 → continuous on RS percentile
- [ ] `longterm_rs_pts` — 0/3/5/8 → continuous on 60d+120d excess return
- [ ] `near_highhist_pts` — 0/3/5 → linear on distance to historical high
- [ ] `bb_upper_walk_pts` — 0/3 → continuous on (near_upper_days / window)

### Task 4.44.5: Pillar 4 + Risk deductions (14 factors)

- [ ] `emerging_setup_pts` (0/10) — keep binary (gate-style)
- [ ] `pullback_setup_pts` (0/8) — keep binary
- [ ] `bb_squeeze_coiling_pts` (0/3) — keep binary
- [ ] All 11 risk deductions — most are gate-style binary penalties; keep as-is unless underlying has natural continuum:
  - [ ] `long_upper_shadow` — continuous on upper_shadow_ratio
  - [ ] `overheat_ma20` / `overheat_ma60` — continuous on distance above MA
  - [ ] `recent_advance_deduction` (0/5/10) — continuous on advance % above threshold
  - [ ] `adx_exhaustion_deduction` (0/6) — continuous on ADX magnitude above 50

### Task 4.44.6: Test migration & verification

- [ ] Re-run full test suite, migrate ~150 additional integer-equality assertions to `pytest.approx(N, abs=0.5)` per affected factor
- [ ] Smoke test: same 4-ticker scan should show even finer score differentiation
- [ ] Verify no win-rate regression via `make backtest`

---

## Task 4.44.7: Update CLAUDE.md

- [ ] Add Phase 4.44 row:

  ```markdown
  | Phase 4.44 | ✅ Done | **TCE 完成連續評分**：剩餘 59 個小桶因子全改連續函數 ✅ · ~150 個測試斷言改 `pytest.approx` ✅ · 真正二元的因子（GATE_PASS、binary flags）保留整數 ✅ |
  ```

---

# Phase 4.45 — SurgeRadar Continuous Scoring

**Goal:** Apply continuous scoring to `surge_radar.py` (24 `_score_*` methods).

**Why separate phase:** SurgeRadar is structurally similar to TCE but has its own `_ScoreBreakdown` and is tested by ~80 unit tests. Doing it in its own phase isolates risk to surge backtesting.

---

## File Map

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/surge_radar.py` | **Modify** — `_ScoreBreakdown` int→float, 24 `_score_*` methods continuous |
| `tests/unit/test_surge_radar*.py` | **Migrate** — ~80 assertions to `pytest.approx` |
| `scripts/surge_scan.py` | **Modify** — display 1-decimal scores |

---

## Task 4.45.1: SurgeRadar `_ScoreBreakdown` int → float

- [ ] Locate the SurgeRadar dataclass (search for `class _ScoreBreakdown` in `surge_radar.py`).
- [ ] Convert all `_pts: int = 0` → `_pts: float = 0.0`.
- [ ] Convert `total` property to return `float`.

## Task 4.45.2: Convert 24 `_score_*` methods to continuous

Order by impact (top first):

**High impact (8 methods — variance drivers):**
- [ ] `_score_vol_ratio` (line 169) — log curve, similar to TCE
- [ ] `_score_close_strength` (line 189) — linear cs × range
- [ ] `_score_inst_buy_fresh` (line 201) — linear on inst_net_buy / avg_vol
- [ ] `_score_inst_cumulative_flow` (line 456) — log curve on cumul/avg
- [ ] `_score_inst_synergy` (line 413) — continuous inst_buy_pct
- [ ] `_score_ownership_concentration` (line 505) — continuous on holdings_chg
- [ ] `_score_breakout_20d` (line 312) — continuous on breakout magnitude
- [ ] `_score_rsi_healthy` (line 325) — triangular on RSI

**Medium impact (10 methods):**
- [ ] `_score_industry_strength` (line 217)
- [ ] `_score_pocket_pivot` (line 234)
- [ ] `_score_breakaway_gap` (line 269)
- [ ] `_score_relative_strength` (line 288)
- [ ] `_score_bb_squeeze_breakout` (line 339)
- [ ] `_score_margin_not_hot` (line 397)
- [ ] `_score_margin_declining` (line 445) — binary, keep
- [ ] `_score_ma5_walk` (line 565)
- [ ] `_score_bb_upper_walk` (line 592)
- [ ] `_score_market_heat` (line 713)

**Low impact (6 methods — gates/binary):**
- [ ] `_score_daytrade_penalty` (line 549) — keep binary
- [ ] `_score_foreign_trend` (line 633) — continuous on W1/W2 ratio
- [ ] `_score_short_cover` (line 648) — continuous
- [ ] `_score_large_2w_trend` (line 661) — continuous
- [ ] `_score_inst_accel_short` (line 678) — continuous
- [ ] `_score_taifex_context` (line 693) — keep binary (gate-style)

## Task 4.45.3: SURGE_ALPHA / SURGE_BETA thresholds

The grade tiers (ALPHA ≥55, BETA ≥40) use integer cutoffs. With float scores, these become "fuzzy" near the boundary.

- [ ] **Decision:** Keep thresholds as-is (55.0 / 40.0). A score of 54.9 is BETA, 55.0 is ALPHA. No special handling needed.
- [ ] **Optional:** Add a `tier_confidence` field showing distance from threshold (e.g., 55.0 = 0% margin into ALPHA, 60.0 = 100% margin).

## Task 4.45.4: DB schema

- [ ] Verify `surge_signals.score` column is numeric (not int). Most likely yes since it's already FLOAT-compatible.
  ```bash
  grep -n "score" db/migrations/010_surge_to_db.sql
  ```
- [ ] No migration needed if already numeric.

## Task 4.45.5: Test migration & smoke test

- [ ] Convert ~80 `surge_radar` tests to `pytest.approx`.
- [ ] Smoke test:
  ```bash
  .venv/bin/python scripts/surge_scan.py --date 2026-05-26
  ```
  Verify scores are now distinct decimals.
- [ ] `make surge-backtest` — confirm no win-rate regression.

## Task 4.45.6: Update CLAUDE.md

- [ ] Add Phase 4.45 row:

  ```markdown
  | Phase 4.45 | ✅ Done | **SurgeRadar 連續評分**：`_ScoreBreakdown` int→float ✅ · 24 個 `_score_*` 改連續（保留 daytrade/taifex 二元）✅ · ~80 surge 測試改 `pytest.approx` ✅ · `surge_signals.score` 已為 numeric 不需 migration ✅ |
  ```

---

# Phase 4.46 — Early-Accumulation & Pullback Detectors

**Goal:** Convert the 5 early-positioning detectors (Pullback, InstAccum, ChipTransfer, VCP, HTF) to continuous scoring.

**Why separate phase:** These detectors use a different pattern — accumulating `score += N` per qualifying condition rather than `_ScoreBreakdown` dataclass. The refactor is straightforward but each detector has its own MIN_SCORE gate.

---

## File Map

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/inst_accum_detector.py` | **Modify** — convert 6 scoring blocks |
| `src/taiwan_stock_agent/domain/chip_transfer_detector.py` | **Modify** — convert K-of-N + 5 chip signals |
| `src/taiwan_stock_agent/domain/vcp_detector.py` | **Modify** — convert contraction scoring |
| `src/taiwan_stock_agent/domain/htf_detector.py` | **Modify** — convert advance/consolidation scoring |
| `src/taiwan_stock_agent/domain/pullback_detector.py` | **Modify** — convert 5–6 scoring blocks |
| `tests/unit/test_*_detector.py` (5 files) | **Migrate** — ~50 assertions to `pytest.approx` |

---

## Task 4.46.1: InstAccumDetector

Current pattern (lines ~73–144):
```python
score = 0
if consec_days >= 8: score += 25
elif consec_days >= 5: score += 18
else: score += 10
# ... 5 more buckets ...
```

**Convert each `if/elif` block to continuous formula.** Examples:

- [ ] **Consec buy days (0–25):** `score += min(25.0, consec_days / 8.0 * 25.0)` (linear: 0d→0, 8d→25, cap)
- [ ] **Distance from 60D high (0–20):** `score += min(20.0, max(5.0, distance_pct * 50.0))` (15%→7.5, 20%→10, 40%→20)
- [ ] **Volume dry-up (0–15):** `score += max(0.0, (0.80 - vol_ratio) * 50.0)` (clamp to [0, 15])
- [ ] **Cumul net buying (0–15):** linear on cumul / avg_vol_20
- [ ] **Chip cleanliness (0–10 or -5):** continuous on `large_holder_chg_pct`
- [ ] **MA alignment (0–10):** continuous on min(MA pair gaps)
- [ ] **MIN_SCORE gate (35) stays the same** — float comparisons work fine.

## Task 4.46.2: ChipTransferDetector

K-of-N (3-of-5) gate becomes "partial credit":

- [ ] Per chip signal, instead of binary +/0, give continuous credit based on signal strength.
- [ ] Total score still gated at MIN_SCORE = 40.0.
- [ ] Verify the K-of-N gate count still requires ≥3 signals firing (use boolean check for "fired" = continuous score > 0).

## Task 4.46.3: VCPDetector

The contraction-detection logic is already continuous (looking at peak/trough ratios). Score assignment is bucketed.

- [ ] Convert contraction scoring to continuous on tightness ratios.
- [ ] Volume dry-up: linear instead of bucket.
- [ ] MA5 > MA60 gate: keep binary (it's a gate, not a score).

## Task 4.46.4: HTFDetector

- [ ] Advance % (currently bucketed at 25%, 40%): continuous from 25% baseline.
- [ ] Consolidation range (currently bucketed at 10%, 15%): continuous (tighter = more).
- [ ] Volume contraction (0.65× threshold): continuous on ratio.

## Task 4.46.5: PullbackDetector

- [ ] Distance from MA20 (currently ±5% gate): keep as gate, but score continuous within range.
- [ ] RSI reset (currently bucketed): continuous on RSI distance from reset zone.
- [ ] Prior advance: linear.
- [ ] Pullback duration: linear on consec_down_days.
- [ ] VOL_BOUNCE: continuous on bounce-day volume vs avg.

## Task 4.46.6: Test migration

- [ ] ~50 unit tests across 5 detector test files. Use `pytest.approx(N, abs=2.0)` (broader tolerance — detectors have lots of additive components).

## Task 4.46.7: Smoke test

- [ ] Full `make plan` scan, verify early-positioning signals (法人建倉/籌碼轉移/VCP/旗形) have distinct float scores.
- [ ] HTML cards: confidence shows 1 decimal, no two cards in same strategy tier are identical.

## Task 4.46.8: Update CLAUDE.md

- [ ] Add Phase 4.46 row:

  ```markdown
  | Phase 4.46 | ✅ Done | **早期佈局偵測器連續評分**：InstAccum/ChipTransfer/VCP/HTF/Pullback 全改連續 ✅ · K-of-N 改 partial credit（門檻仍要求 N 個信號 fired）✅ · ~50 detector 測試改 `pytest.approx` ✅ · 全引擎完成連續評分，分數撞群問題消除 ✅ |
  ```

---

# Overall Rollout

| Phase | Effort | Touches | Risk |
|-------|--------|---------|------|
| 4.43 | ~3 hrs | TCE 8 high-variance + display | Low (mechanical) |
| 4.44 | ~4 hrs | TCE 59 small-bucket factors | Low (per-factor isolated) |
| 4.45 | ~3 hrs | SurgeRadar 24 methods | Medium (surge backtest sensitivity) |
| 4.46 | ~3 hrs | 5 early-positioning detectors | Medium (newest code, fewer tests) |

**Total:** ~13 hours of focused work. Each phase ships independently — can be reverted without affecting prior phases.

**Sequencing recommendation:** Do 4.43 first and ship. Observe in production for 2–3 days. If no regression, proceed with 4.44 → 4.45 → 4.46 in sequence.

**Stop criteria for each phase:**
- Tests pass (full `make test`).
- `make backtest` (TCE) / `make surge-backtest` (SurgeRadar) shows win-rate within ±1% of pre-change baseline.
- HTML smoke test: 10 cards in same strategy tier, no two identical confidence values.
