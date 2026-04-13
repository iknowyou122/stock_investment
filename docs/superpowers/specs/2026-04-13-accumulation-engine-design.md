# Accumulation Engine Recalibration

**Date:** 2026-04-13
**Status:** Design approved, pending implementation

## Problem

The current Triple Confirmation Engine v2 selects stocks that are **already in a strong uptrend** (momentum chasing). Backtest data from 312,307 signals shows higher confidence scores correlate with **lower** T+1 win rates:

| Score range | Win rate | Avg return |
|-------------|----------|------------|
| 50-54       | 46.2%    | +0.52%     |
| 55-59       | 45.3%    | +0.36%     |
| 60-64       | 41.4%    | +0.29%     |
| 68+         | 40.5%    | +0.26%     |

Root cause: high-scoring factors (20d breakout, RSI 55-70, trend continuity) select stocks at the **end** of their move, not the beginning.

## Goal

Recalibrate the engine to detect stocks **about to break out or entering the next surge**, not stocks already running. Two target patterns:

1. **Accumulation breakout** (蓄積突破): consolidation + shrinking volume + institutional buying + MA alignment forming, about to explode
2. **Pullback setup** (初升段回踩): already surged once, pulled back to MA20 support with volume contraction, second wave about to start

**Entry timing:** Pre-breakout (1-2 days before), aligned with existing T+2 strategy.
**Signal target:** 5-10 LONG per day, win rate 50%+.

## Changes

### 1. Fix negative-lift factors

Per-factor lift analysis on backtest data:

| Factor | Lift | Action |
|--------|------|--------|
| rsi_momentum_pts | -3.7pp | Shift range from 55-70 to **30-55** (healthy recovery, not overheated) |
| close_strength_pts | -3.6pp | Reduce: 0.5-0.7 = +4, >=0.7 = +2 (was +4), <0.5 = 0 |
| overheat_ma20 | +11.9pp | **Remove** -5 deduction (data shows it's a positive signal) |
| overheat_ma60 | +9.6pp | **Remove** -5 deduction |

**Files:** `triple_confirmation_engine.py` (scoring methods + risk deductions)

### 2. Gate: add institutional accumulation condition

Current Gate requires 2-of-4. Condition 3 (close >= 20d high) blocks all consolidating stocks.

**Add Gate condition 5:** Foreign or trust net buy on >= 2 of last 3 trading days.

Gate becomes **2-of-5**. Accumulating stocks can pass via "VWAP + institutional" or "relative strength + institutional" without needing a 20d breakout.

**Data source:** Existing TWSE T86 free-tier chip data (institution_continuity calculation already exists in free-chip scoring).

**Files:** `triple_confirmation_engine.py` (`_gate_check` method)

### 3. New Pillar 4: Accumulation Detection

New scoring block alongside existing Pillars 1-3.

#### 3a. EMERGING_SETUP scoring (+10 pts)

Existing EMERGING_SETUP flag detection (MA aligned + institutional buy + not broken out) promoted from flag-only to scored factor.

```
Conditions (all must be true):
  - MA5 > MA10 > MA20 (bullish alignment)
  - MA20 rising vs 5 sessions ago
  - Foreign or trust net buy present
  - close < twenty_day_high * 0.99 (NOT yet broken out)
Score: +10 pts
Flag: EMERGING_SETUP
```

#### 3b. PULLBACK_SETUP scoring (+8 pts)

New factor detecting first-wave pullback to support.

```
Conditions (all must be true):
  - Touched 20d high within last 20 sessions (had a breakout)
  - Current close within MA20 +/- 3% (pulled back to MA support)
  - MA20 still rising (trend intact)
  - Last 3 days volume < 20d avg * 0.8 (volume contraction = weak selling)
Score: +8 pts
Flag: PULLBACK_SETUP
```

EMERGING and PULLBACK are mutually exclusive (one requires no breakout, the other requires prior breakout).

#### 3c. BB_SQUEEZE_COILING bonus (+3 pts)

Additional bonus when BB squeeze coincides with extreme volume contraction.

```
Conditions:
  - BB_SQUEEZE_SETUP already triggered (bb_width_pct < 20)
  - Last 3 days volume < 20d avg * 0.7 (extreme contraction)
Score: +3 pts (on top of existing bb_squeeze_breakout_pts)
Flag: BB_SQUEEZE_COILING
```

**Pillar 4 effective max:** 13 pts (max(10, 8) + 3).

**Files:** `triple_confirmation_engine.py` (new `_accumulation_score` method + `_ScoreBreakdown` fields)

### 4. Threshold adjustment

| Regime | Old | New |
|--------|-----|-----|
| Uptrend | 63 | **50** |
| Neutral | 68 | **55** |
| Downtrend | 73 | **60** |
| WATCH min | 45 | **40** |

**Files:** `config/engine_params.json`

### 5. New tunable parameters

Add to `engine_params.json`:

```json
{
  "emerging_setup_pts": 10,
  "pullback_setup_pts": 8,
  "bb_squeeze_coiling_pts": 3,
  "rsi_accumulation_lo": 30,
  "rsi_accumulation_hi": 55
}
```

Update `scoring_replay.py` `recompute_score()` to support these new parameters in grid search.

**Files:** `config/engine_params.json`, `src/.../scoring_replay.py`

## Files to modify

| File | Changes |
|------|---------|
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | Pillar 4 factors, Gate 5, RSI/close_strength fix, remove overheat deductions |
| `config/engine_params.json` | New thresholds + accumulation params |
| `src/taiwan_stock_agent/domain/scoring_replay.py` | `recompute_score` support for new params |
| `tests/unit/test_triple_confirmation_engine_v2.py` | ~10 new tests |

No new files. No migration needed (no DB schema changes).

## Verification

### Re-run backtest

```bash
make backtest DATE_FROM=2025-08-01 DATE_TO=2026-04-08
```

### Success criteria

| Metric | Old engine | Target |
|--------|-----------|--------|
| LONG signals/day | ~0.3 | 5-10 |
| T+1 win rate | ~40% | >= 48% |
| Avg T+1 return | +0.26% | >= +0.35% |
| EMERGING_SETUP trigger rate | flag only | > 5% of gate-passing stocks |
| PULLBACK_SETUP trigger rate | 0 (new) | > 3% of gate-passing stocks |

### Tests

- All existing 242 unit tests pass
- New tests: EMERGING_SETUP scoring, PULLBACK_SETUP each condition, Gate 5 institutional, RSI new range, close_strength new tiers, overheat removal
