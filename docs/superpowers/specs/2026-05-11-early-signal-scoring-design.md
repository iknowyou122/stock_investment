# Early Signal Scoring Improvements — Design Spec

**Date:** 2026-05-11  
**Status:** Approved  
**Motivation:** 信昌電 (6173) post-mortem — system gave WATCH 47 on 2026-04-13 at 71元 and only LONG 73 on 2026-04-16 at 83元 (+17% late). Three structural causes identified; this spec addresses all three.

---

## Root Cause Summary

| Cause | Impact |
|-------|--------|
| Sector rank flat +5 for all top-20% | top-7.7% stock gets same bonus as top-19% |
| Trajectory/persist bonus is zero on day 1 | First-appearance stocks systemically disadvantaged |
| LONG threshold doesn't reward near-high coil quality | 92-99% zone stocks need to break out before threshold drops |

---

## Fix 1: Sector Rank Tiered Bonuses

**File:** `scripts/batch_plan.py` → `_apply_sector_ranks()`

### Current behaviour
Top 20% of sector → flat +5 pts. SECTOR_RANK flag commented out.

### New behaviour
Three tiers computed from the sorted-by-confidence sector list:

| Tier | Threshold | Bonus |
|------|-----------|-------|
| Elite | top 5% (`len // 20`) | +10 |
| Strong | top 10% (`len // 10`) | +7 |
| Warm | top 20% (`len // 5`) | +5 |

SECTOR_RANK:N/M flag re-enabled so rank is visible in CSV output.

### Constraints
- Only applied when sector has ≥ 3 valid (non-halt, non-error) results (unchanged).
- Each ticker gets the highest tier it qualifies for.
- Cap remains `min(100, confidence + bonus)`.

---

## Fix 2: Near-High Coil First-Day Bonus

**File:** `scripts/batch_plan.py` → new `_apply_near_high_first_day()`

### Problem
`_apply_persist_bonus()` skips tickers that don't appear in yesterday's CSV — i.e. every stock on its first scan day gets 0 trajectory bonus. But a stock in the 92-99% zone (proximity_pts = 12) is already demonstrating tight accumulation near resistance; punishing it for being new distorts the signal.

### New behaviour
- Load yesterday's CSV (lookback=1).
- For each ticker **not in yesterday's CSV** (true first appearance):
  - If `proximity_pts == 12` → apply **+4 pts** + `NEAR_HIGH_COIL` flag.
- Tickers that already received a persist bonus are unaffected (called after persist).

### Data plumbing
`proximity_pts` is already serialised in `score_breakdown["pts"]` by `strategist_agent.py` (line 186-204, `dataclasses.asdict(breakdown)`). Add one line to the result dict in `_run_ticker()`:

```python
"proximity_pts": breakdown_pts.get("proximity_pts", 0),
```

Also add `institution_continuity_pts` to the ERROR fallback dict (currently missing, causes KeyError risk in `_apply_catalyst_filter`).

### Call order in `run_batch`
1. `_apply_sector_ranks()` — Fix 1
2. `_apply_persist_bonus()` — existing
3. `_apply_near_high_first_day()` — Fix 2 (new, after persist)
4. `_apply_catalyst_filter()` — existing

---

## Fix 3: Proximity-Adjusted LONG Threshold

**File:** `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` → `_map_action()`

### Problem
A stock in the 92-99% zone (proximity_pts = 12) is, by definition, a confirmed pre-breakout coil. The current LONG threshold ignores this structural quality and treats it identically to any other score.

### New behaviour
When `bd is not None` and `bd.proximity_pts == 12`:

| Regime | Current threshold | New threshold |
|--------|-------------------|---------------|
| uptrend | 60 | 55 |
| neutral | 65 | 60 |
| downtrend | 70 | 70 (unchanged — downtrend requires caution) |

### Implementation
`_map_action()` already receives `bd: _ScoreBreakdown | None`. Add a proximity check after regime assignment:

```python
if bd is not None and bd.proximity_pts == 12:
    long_threshold = max(long_threshold - 5, _WATCH_MIN + 1)
```

The `max(..., _WATCH_MIN + 1)` guard ensures LONG threshold never drops below WATCH + 1 pt.

---

## Expected Impact (6173 retrospective)

| Fix | Score change |
|-----|-------------|
| Fix 1 (rank 15/195 → top 10% → +7 vs +5) | +2 |
| Fix 2 (first day, proximity=12 → +4) | +4 |
| Fix 3 (uptrend threshold 60 → 55) | −5 gap |
| **Total** | **score 53 vs threshold 55 → WATCH (−2)** |

The fixes don't retroactively flip 4/13 to LONG but eliminate the structural penalties. Combined with a marginally stronger real score (which we cannot precisely reconstruct), the signal would likely have been LONG on day 1 or 2 instead of day 4.

---

## Test Requirements

- `test_apply_sector_ranks_tiered`: verify +10/+7/+5 tiers with fixture sectors of ≥20 stocks.
- `test_apply_near_high_first_day`: verify +4 applied to proximity=12 first-day ticker; verify NO bonus when ticker was in yesterday CSV; verify no double-count with persist.
- `test_map_action_proximity_threshold`: verify uptrend threshold = 55 when proximity=12; neutral = 60; downtrend = 70.
- Existing tests must remain green (no regressions).

---

## Out of Scope

- Changing the gate conditions (G1–G5) themselves.
- Modifying Pillar 1/2 scoring.
- Any UI/bot changes.
