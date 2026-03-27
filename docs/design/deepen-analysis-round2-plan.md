# Plan: Triple Confirmation Engine — Round 2 Factor Deepening
<!-- /autoplan restore point: /Users/07683.howard.huang/.gstack/projects/stock_investment/main-autoplan-restore-round2.md -->

Generated: 2026-03-26 | Branch: main | Repo: stock_investment

## Objective

Round 1 (`enhance-analysis-factors-plan.md`, committed 7c7a29c) added 8 new factors across all three pillars.
This plan performs a **second-pass deepening** based on three observations from the implemented code:

1. **Volume surge scores without direction** — `_volume_surge_score` awards +20 for any high-volume day, including distribution days (大量下殺). This inflates scores on bearish setups.
2. **Space pillar only looks 20 sessions back** — detecting a quarterly (60d) breakout is materially stronger than a monthly (20d) breakout but requires extending the history window.
3. **`high52w_pct` field exists in `_AnalysisHints` but is never computed** — a 52-week proximity hint is valuable LLM context for gauging resistance overhead.

Additionally, one new scoring factor is proposed for momentum:
4. **Volume accumulation trend (量能遞增)** — 3 consecutive sessions of increasing volume measures the TREND of participation, orthogonal to the existing LEVEL check (volume > 20d avg × 1.5).

---

## Current State: Factor Audit (post Round 1)

| Pillar | Factors | Max pts | Notes |
|--------|---------|---------|-------|
| P1 Momentum | VWAP-5d, volume surge, close strength, consec up | 50 | Bug: volume surge is directionally agnostic |
| P2 Chip (paid) | net_buyer_diff, concentration_top15, no_daytrade | 40 | Gated by Phase 1 |
| P2 Chip (free) | foreign, trust, dealer, margin, all-inst, foreign consec | 50 | TWSE opendata |
| P3 Space | 20d high, MA alignment, MA20 slope, RS vs TAIEX | 35 | Window = 35 calendar days |
| Risk | daytrade −25, short spike −10, divergence −15 (P4) | — | — |

---

## Premises (must hold for plan to be valid)

1. **Distribution day is not accumulation.** High volume on a down day (close < prev_close) indicates institutions selling, not buying. Awarding +20 momentum points for this is a scoring error — not a debatable design choice.
2. **60d breakout is a stronger signal than 20d breakout.** A stock breaking a 3-month high has cleared more resistance and attracted broader institutional attention than one at a 1-month high. The distinction is measurable and orthogonal.
3. **Volume TREND (3-day increasing) is orthogonal to volume LEVEL (today > 20d avg).** A 3-day consecutive increase can occur without crossing the 1.5× threshold, and vice versa. These measure different market phenomena.
4. **Extending OHLCV fetch from 35 to 95 calendar days is safe.** FinMind rate limits are per-API-call, not per-row. Fetching 90 days vs 30 days of OHLCV doubles rows per call (from ~20 to ~60 rows) but stays well within the batch scan's 4-worker parallel architecture.

---

## Proposed Changes

### Change A: Volume Surge — Direction Fix (Bug Fix)

**File:** `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

**Current behavior:** `+20 if volume > 20d_avg × 1.5` (direction-agnostic)

**New behavior:** `+20 if volume > 20d_avg × 1.5 AND close >= prev_close`
                  `0  if volume > 20d_avg × 1.5 AND close < prev_close` (distribution day → no bonus)

**Method:** `_volume_surge_score(self, ohlcv, history)` — add `prev_close` lookup from sorted history.

**Flag added:** `"VOLUME_DISTRIBUTION"` to `breakdown.flags` when surge is on a down day.

**Score impact:** Distribution day loses its spurious +20. No new scoring fields required.

**Data required:** `ohlcv_history` (already passed in). Read `sorted_history[-1].close` as `prev_close`.

---

### Change B: Volume Accumulation Trend (New +5 Factor)

**File:** `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

**New field:** `volume_trend_pts: int = 0` in `_ScoreBreakdown.total` sum.

**Logic:** `+5 if volume[-1] > volume[-2] > volume[-3]` (3 consecutive sessions of increasing volume, NOT including today)

Note: this checks the PRIOR 3 sessions only (not today), so today's surge doesn't inflate the trend score.
Requires ≥ 4 sessions in history (3 prior + 1 for today's OHLCV).

**Orthogonality check:**
- `volume_surge_pts` = level check: today's volume vs 20d average
- `volume_trend_pts` = trend check: are the last 3 days' volumes sequentially increasing?
- A stock can have trend (3-day increase) WITHOUT triggering surge (none above 1.5× avg)
- A stock can trigger surge (1 big day) WITHOUT trend (no prior buildup)
- Measured correlation: weak (independent phenomena)

**New `_PILLAR1_MAX`:** 50 → 55

---

### Change C: Extend OHLCV History + 60d High Breakout (New +10 Factor)

**Files touched:**
1. `src/taiwan_stock_agent/agents/strategist_agent.py` — change `timedelta(days=35)` → `timedelta(days=95)`
2. `src/taiwan_stock_agent/domain/models.py` — add `sixty_day_high: float = 0.0` and `sixty_day_sessions: int = 0` to `VolumeProfile`
3. `src/taiwan_stock_agent/agents/strategist_agent.py` — update `_build_volume_profile()` to compute 60d high
4. `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` — add `sixty_day_high_pts: int = 0` to `_ScoreBreakdown`, add `_sixty_day_high_score()`, call in `_compute()`

**New `_build_volume_profile` logic:**
```
recent_20 = sorted_history[-20:]   # existing
recent_60 = sorted_history[-60:]   # new (up to 60 sessions if available)
sixty_day_high = max(d.high for d in recent_60) if recent_60 else 0.0
sixty_day_sessions = len(recent_60)
```

**Scoring:** `+10 if close > sixty_day_high × 0.99` (within 1% of or above 60d high)

**Combined space scoring interpretation:**
```
close > 60d_high × 0.99:  +10 pts (quarterly breakout — strong)
close > 20d_high × 0.99:  +20 pts (monthly breakout)
Both:                      +30 pts total (both fire simultaneously for genuine breakouts)
```

**New `_PILLAR3_MAX`:** 35 → 45

**Flag if insufficient history:** `"INSUFFICIENT_HISTORY_60D_HIGH"` when sixty_day_sessions < 40 (less than 2 months of data)

**Cache impact:** Parquet cache keys include `(dataset, ticker, start, end)`. Extending the window changes `start` so existing caches are automatically invalidated for the wider range. No manual cache purge needed.

---

### Change D: Compute high52w_pct Hint (Hints Fix)

**File:** `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

**Status:** `_AnalysisHints.high52w_pct` field exists but `_compute_hints()` never computes it.

**Fix:** In `_compute_hints()`, compute from available history:
```python
# high52w_pct: (close - 52w_high) / 52w_high
# Use longest available window (may be <252 sessions in Phase 1-3)
all_highs = [d.high for d in sorted_history]
if all_highs:
    period_high = max(all_highs)
    if period_high > 0:
        hints.high52w_pct = round((ohlcv.close - period_high) / period_high * 100, 2)
```

**Note:** With 60+ sessions available (after Change C), this gives a "60d high proximity" rather than a true 52w proximity. Still meaningful for LLM context (how far from recent peak). The field name `high52w_pct` is aspirational; Phase 4+ can upgrade with real 52w data.

**Update `StrategistAgent._format_hints_for_prompt()`:** Add `high52w_pct` hint formatting.

---

## Data Flow Diagram

```
StrategistAgent.run(ticker, analysis_date)
        │
        ├── fetch_ohlcv(start = analysis_date - 95d, end = analysis_date)
        │         └── returns ~60 trading sessions (up from ~25)
        │
        ├── _build_volume_profile(ticker, analysis_date, history)
        │         ├── twenty_day_high = max(recent[-20:].high)   [existing]
        │         └── sixty_day_high  = max(recent[-60:].high)   [NEW Change C]
        │
        └── TripleConfirmationEngine.score_full(ohlcv, history, chip, profile, proxy, taiex)
                  │
                  ├── Pillar 1: Momentum
                  │   ├── _vwap_score()           +20 [existing]
                  │   ├── _volume_surge_score()   +20 [MODIFIED: direction check]
                  │   ├── _close_strength_score() +5  [existing]
                  │   ├── _consec_up_score()       +5  [existing]
                  │   └── _volume_trend_score()    +5  [NEW Change B]
                  │
                  ├── Pillar 2: Chip [unchanged]
                  │
                  └── Pillar 3: Space
                      ├── _space_score()          +20  (20d high) [existing]
                      ├── _sixty_day_high_score()  +10  (60d high) [NEW Change C]
                      ├── _ma_alignment_score()    +5   [existing]
                      ├── _ma20_slope_score()      +5   [existing]
                      └── _rs_score()              +5   [existing]
```

---

## Score Impact Analysis

| Scenario | Before | After | Delta |
|----------|--------|-------|-------|
| Distribution day (high vol, down) | 20 (wrong) | 0 | −20 |
| Accumulation day (high vol, up) | 20 | 20 | 0 |
| 3-day volume trend present | 0 | 5 | +5 |
| 60d high breakout (also 20d high) | 20 | 30 | +10 |
| 60d high breakout (NOT 20d high) | 0 | 10 | +10 |
| All new factors firing (accumulation) | — | +15 bonus | — |

Distribution day fix has the biggest behavioral impact: a stock with high-volume sell-off would previously score up to 20 pts for "momentum" — this correction prevents false LONG signals on 大量下殺 days.

---

## Test Plan

### New tests required:

**`test_volume_surge_direction.py` (or add to existing volume tests):**
1. `test_volume_surge_accumulation()` — surge + close > prev_close → +20 ✓
2. `test_volume_surge_distribution()` — surge + close < prev_close → 0 + "VOLUME_DISTRIBUTION" flag
3. `test_volume_surge_no_history()` — no history → 0 + "INSUFFICIENT_HISTORY" flag
4. `test_volume_surge_flat()` — surge + close == prev_close → +20 (flat is not distribution)

**`test_volume_trend_score.py`:**
1. `test_volume_trend_three_days()` — 3 increasing days → +5
2. `test_volume_trend_plateau()` — flat then up → 0
3. `test_volume_trend_insufficient_history()` — <4 sessions → 0
4. `test_volume_trend_today_excluded()` — trend is computed from prior sessions only

**`test_sixty_day_high_score.py`:**
1. `test_sixty_day_high_fires()` — 60 sessions, close near max → +10
2. `test_sixty_day_high_insufficient()` — <40 sessions → 0 + INSUFFICIENT flag
3. `test_sixty_day_above_twenty()` — 60d > 20d (different highs) → both fire (30 pts total)
4. `test_sixty_day_same_as_twenty()` — few sessions, 60d == 20d → only 20d fires (20 pts)

**`test_hints_high52w_pct.py`:**
1. `test_high52w_below_recent_high()` — close < period high → negative pct
2. `test_high52w_at_high()` — close at period high → 0.0
3. `test_high52w_no_history()` — empty history → None

**Integration test:**
1. `test_distribution_day_does_not_fire_long()` — high-volume down day with all other factors should not produce LONG

---

## NOT in Scope

- **52w true breakout signal as scoring factor**: Requires 250 sessions (~1 year). Not feasible in Phase 1-3 data window even with 95-day fetch. Deferred to Phase 4.
- **ATR contraction (pre-breakout coiling)**: Requires 20+ sessions of ATR measurement. Can compute with 60-session history but the "coiling" signal (ATR contraction before expansion) is complex to validate. Deferred to P3 as a future hint.
- **Upper shadow rejection deduction**: Correlated with close_strength (both measure close position in range). Adding both would double-count. Skip.
- **Active distribution deduction (−10 for distribution day)**: Change A already removes the spurious +20. Adding an extra −10 penalty is Phase 4 work requiring tick data to distinguish institutional distribution from retail panic. For now, neutral (0) is correct.
- **Rebalancing total point weights**: Total theoretical max (paid: ~135, free: ~145) both exceed 100 and rely on the `min(100, raw)` cap. Rebalancing to sum exactly to 100 would require changing all existing thresholds and breaking backtest baselines. Deferred.
- **Dealer consecutive buy days**: Only 外資連買天數 is tracked by ChipProxyFetcher. Adding 投信/自營商 consecutive days requires multi-day TWSE T86 lookback. Out of scope for now.

---

## What Already Exists

- `_AnalysisHints.high52w_pct` field — exists, uncomputed. Change D wires it up.
- `_AnalysisHints.gap_down_pct` — exists, computed. Pattern for adding high52w_pct.
- `StrategistAgent._format_hints_for_prompt()` — has pattern for adding new hint formatting.
- `VolumeProfile.twenty_day_high` — exists. Pattern for adding `sixty_day_high`.
- Parquet cache infrastructure — auto-invalidates on window change. No manual work needed.

---

## Implementation Order

1. Change A (direction fix) — smallest diff, highest correctness impact, no new model fields
2. Change B (volume trend) — new `_ScoreBreakdown` field + 1 method
3. Change C (60d high) — requires coordinated change across 3 files + model extension
4. Change D (hint fix) — 5-line change in `_compute_hints()` + prompt formatting

---

## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|
| 1 | CEO | Volume direction fix = bug fix, not debatable design | P5 (explicit) | High-volume down day is distribution by definition; awarding +20 is wrong | Adding deduction: deferred (neutral is safer) |
| 2 | CEO | 60d breakout adds genuine orthogonal signal | P1 (completeness) | Monthly vs quarterly breakout = different significance | 120d/yearly: requires 250+ sessions, out of Phase 1-3 reach |
| 3 | CEO | Volume trend is orthogonal to volume level | P5 (explicit) | Level = spike vs baseline; trend = 3-day sequential increase | RSI-based trend: correlated with VWAP (ruled out in Round 1) |
| 4 | CEO | Extend OHLCV window 35→95 calendar days | P1 (completeness) | 95d → ~60 sessions, sufficient for 60d high; minimal FinMind overhead | 250d+ window: too large for batch scan performance |
| 5 | Eng | No new deduction field for distribution day | P5 (explicit/minimal-diff) | Removing spurious +20 is sufficient; deduction is Phase 4 territory | −10 deduction: requires tick data for institutional vs retail attribution |
| 6 | Eng | `high52w_pct` = "60d high pct" in Phase 1-3 | P3 (pragmatic) | Field name is aspirational; with 60 sessions, it's a 3-month proxy. Still useful. | Rename field: breaks API contract, not worth it |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

**VERDICT:** NO REVIEWS YET — autoplan pipeline running.
