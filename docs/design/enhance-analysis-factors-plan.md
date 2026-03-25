# Plan: Enhance Triple Confirmation Analysis Factors
<!-- /autoplan restore point: /Users/07683.howard.huang/.gstack/projects/iknowyou122-stock_investment/main-autoplan-restore-20260325-151937.md -->

Generated: 2026-03-25 | Branch: main | Repo: stock_investment

## Objective

重新審視並增加 TripleConfirmationEngine 分析依據，依三個層面強化：
1. **動能面 (Momentum)** — 目前 2 個因子，最高 40 分
2. **籌碼面 (Chip)** — 目前 3 個因子，最高 40 分（依賴 FinMind 付費方案）
3. **空間面 (Space)** — 目前 1 個因子（20日高點代理），最高 20 分

## Current State: Factor Audit

### Pillar 1: Momentum (0–40 pts)
| Factor | Points | Data Source | Status |
|--------|--------|-------------|--------|
| close > vwap_5d | +20 | FinMind free (TaiwanStockPrice) | ✅ Implemented |
| daily_volume > 20d_avg × 1.5 | +20 | FinMind free | ✅ Implemented |

### Pillar 2: Chip (0–40 pts)
| Factor | Points | Data Source | Status |
|--------|--------|-------------|--------|
| net_buyer_count_diff > 0 | +15 | FinMind paid (TaiwanStockBrokerTradingStatement) | ⏳ Phase 1 gate |
| concentration_top15 > 0.35 | +15 | FinMind paid | ⏳ Phase 1 gate |
| no 隔日沖 in top-3 buyers | +10 | FinMind paid + BrokerLabelClassifier | ⏳ Phase 1 gate |
| 隔日沖 in top-3 deduction | -25 | FinMind paid | ⏳ Phase 1 gate |
| momentum divergence deduction | -15 | FinMind tick (external_buy_ratio) | 🔒 Phase 4 |

### Pillar 3: Space (0–20 pts)
| Factor | Points | Data Source | Status |
|--------|--------|-------------|--------|
| close > twenty_day_high × 0.99 | +20 | FinMind free | ✅ Implemented |

### Critical Gap
Without FinMind paid plan:
- Max achievable score = **60 pts** (Pillars 1 + 3 only)
- LONG threshold = **70 pts**
- Result: LONG signal is **unreachable** on free plan

## CEO Review Findings (2026-03-25)

**Dual voice consensus (Codex + Claude subagent, independent):** CONFIRMED on 5/6 dimensions.

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Premises valid?                   ❌      ❌     CONFIRMED: original premises flawed
  2. Right problem to solve?           ✅      ✅     CONFIRMED: LONG gap is real
  3. Scope calibration correct?        ❌      ❌     CONFIRMED: proposed fix is a hack
  4. Alternatives sufficiently explored?❌     ❌     CONFIRMED: TWSE proxy not considered
  5. Competitive/market risks covered? ✅      ✅     CONFIRMED: free-tier constraint real
  6. 6-month trajectory sound?         ✅      ✅     CONFIRMED: defer rebalance is right
═══════════════════════════════════════════════════════════════
CONFIRMED = both agree. DISAGREE = models differ.
```

**Key finding:** Adding RSI/MACD/MA20-streak to Pillar 1 scoring is a **scoring hack** — all derive from
the same OHLCV price series as existing `close > vwap_5d` and `volume > 20d_avg`. Correlated signals
inflate the score without adding predictive independence. Fix the LONG gap with genuinely orthogonal data.

**Premise gate:** User confirmed all 3 revised premises (2026-03-25).

---

## Revised Enhancement Plan (Post-CEO-Review)

### 動能面 (Momentum) — NO SCORING CHANGES

Pillar 1 stays at max 40 pts. RSI/MACD/MA20-streak moved to **LLM reasoning hints** only.

| Metric | Role | Data |
|--------|------|------|
| RSI(14) | LLM hint: flag if overbought (>70) or oversold (<30) | OHLCV free |
| MACD line vs signal | LLM hint: flag golden/dead cross | OHLCV free |
| Close vs MA20 + MA20 slope | LLM hint: trend context | OHLCV free |
| 3-day volume MA rising | LLM hint: participation growing | OHLCV free |

These are computed and passed to `StrategistAgent` as `momentum_hints` — not added to `_ScoreBreakdown`.

### 籌碼面 (Chip) — Free-tier Proxies via TWSE Public Data

New `ChipProxyFetcher` uses TWSE open REST API (no token, no paid plan):

| Factor | Points | Rationale | Data Source |
|--------|--------|-----------|-------------|
| 外資買賣超 > 0 (foreign net buy) | +15 | Institutional directional flow, structurally independent from price | TWSE opendata free |
| 融資餘額 change ≤ 0 (margin longs not increasing) | +10 | Retail not chasing; smart money not distributing to retail | TWSE opendata free |

**Free-tier Pillar 2 max = +25 pts.** Combined with Pillar 1 (40) + Pillar 3 (20) = **85 pts max** on free tier.

**Gate:** Existing FinMind paid factors (`net_buyer_count_diff`, `concentration_top15`, `no_daytrade_top3`)
remain gated behind `chip_data_available` flag and are used when FinMind paid plan is active.
TWSE proxies are the free-tier fallback, not a replacement.

### 空間面 (Space) — One Targeted Addition

| Factor | Points | Rationale | Data |
|--------|--------|-----------|------|
| MA20 slope > 0 (MA20 is rising) | +5 | Trend direction qualifier for existing 20d-high factor | OHLCV free |

Space stays at max **25 pts** (existing 20 + new 5). Gap-down and 52-week-high moved to **LLM hints** only.

### Free-Tier Threshold Mode

New `free_tier_mode: bool` parameter on `TripleConfirmationEngine`:
- When `True`: LONG threshold = **55 pts** (down from 70), output includes `free_tier=True` label
- When `False` (default): LONG threshold = 70 pts (unchanged)
- Label clearly propagates to `SignalOutput.free_tier_mode: bool`

This preserves the signal's semantic meaning: "we believe this is a LONG, but on free-tier proxy data."

### Paid-tier Chip Additions (Phase 1 gate unchanged)

| Factor | Points | Rationale | Data |
|--------|--------|-----------|------|
| Volume-weighted net diff > 0 | replaces net_buyer_count_diff | Economic weight not just count | FinMind paid |
| Top-3 buyers are 波段贏家 or 地緣券商 | +10 bonus | Buyer quality | Phase 2 labels |
| Consecutive buying by same branches (3d) | +5 | Persistence signal | FinMind paid |

---

## Architecture Considerations

1. **Score rebalancing DEFERRED**: Pillar weight changes require Phase 1 spike validation first (71 tests)
2. **Backward compatibility**: `free_tier_mode=False` default preserves all existing callers unchanged
3. **TWSE proxy independence**: `ChipProxyFetcher` is a new infrastructure module, no overlap with `FinMindClient`
4. **LLM hints**: New `_AnalysisHints` separate dataclass (not part of `_ScoreBreakdown`) — fields never affect `total` property
5. **`chip_data_available` flag**: Existing gate on Pillar 2 FinMind factors; TWSE proxies are free-tier fallback only

---

## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|
| 1 | CEO | RSI/MACD/MA20-streak → LLM hints, NOT scoring | P5 Explicit | Correlated with existing VWAP+volume; adds noise not signal | Add to Pillar 1 scoring (+10/+10/+5/+5) |
| 2 | CEO | Add TWSE 外資+融資 as Pillar 2 free-tier proxies | P1 Completeness | Orthogonal to price action; TWSE REST is free, no auth | Use only FinMind paid chip data |
| 3 | CEO | Defer Pillar weight rebalancing (Momentum→60 pts) | P6 Bias toward action | Breaks 71 tests without Phase 1 empirical validation | Rebalance now and rewrite tests |
| 4 | CEO | Add free_tier_mode with LONG threshold=55 | P3 Pragmatic | Makes LONG reachable on free plan with explicit labeling | Raise threshold or lower all thresholds globally |
| 5 | CEO | Space: MA20 slope +5 pts only; gap-down/52w→hints | P5 Explicit | Minimal scoring change; contextual metrics better as hints | Add all 3 Space factors (+10+5+5) to scoring |
| 6 | CEO | New ChipProxyFetcher infra module for TWSE data | P4 DRY | Keeps TWSE and FinMind concerns separate | Add to FinMindClient (mixes auth models) |
| 7 | CEO/Gate | Premises confirmed by user | User gate | Mandatory premise gate — non-auto-decided | — |
| 8 | Eng | LONG guard: block LONG when chip_pts=0 + free_tier_mode=True | P5 Explicit | chip_pts=0 means TWSE failed; score>55 without chip is false signal | Allow LONG when chip data unavailable |
| 9 | Eng | _AnalysisHints as separate dataclass, not fields on _ScoreBreakdown | P5 Explicit | Prevents hint fields from leaking into total property; critical isolation | Add hints as optional fields on existing _ScoreBreakdown |
| 10 | Eng | SignalOutput.free_tier_mode: bool \| None = None (tri-state) | P2 Backward compat | None=legacy callers unaffected; True/False explicit for new consumers | bool with default False (breaks existing callers) |
| 11 | Eng | ChipProxyFetcher injected into StrategistAgent.__init__() | P5 Explicit | Testable via mock; no surprise HTTP calls in tests | Instantiate inside StrategistAgent.run() |
| 12 | Eng | MA20 slope = (MA20[-1] - MA20[-5]) / MA20[-5], require 24 sessions | P5 Explicit | 5-day diff smooths noise; 24 = 20 MA + 4 grace; None if insufficient | Single-day diff (too noisy) |
| 13 | Eng | TWSE ChipProxyFetcher: 24h TTL Parquet cache, same pattern as FinMindClient | P4 DRY | Scan loops (100+ tickers) need cache; reuses existing pattern | No cache (100+ HTTP calls per scan) |
| 14 | Eng | paid chip data available → TWSE proxies skipped entirely (mutual exclusion) | P5 Explicit | No stacking; prevents score > 100; semantic clarity | Allow both to contribute (double-count risk) |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | clean | 5 proposals, 5 accepted, 0 deferred |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | clean | 9 issues, 2 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | (no UI scope) |

**CEO VOICES:** Codex + Claude subagent independent, 5/6 CONFIRMED — key finding: RSI/MACD as scoring is a correlated-signal hack.
**ENG VOICES:** Codex + Claude subagent independent, 5/6 CONFIRMED — Codex-only critical: LONG guard required when chip_pts=0.
**CROSS-PHASE THEME:** Both CEO and Eng voices independently identified LONG threshold gap as the most critical issue.
**UNRESOLVED:** 0 decisions unresolved.
**VERDICT:** CEO + ENG CLEARED — ready to implement.

