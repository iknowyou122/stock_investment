# Plan: Chip Factor Expansion — 籌碼面全面深化

Generated: 2026-03-27 | Branch: main | Repo: stock_investment

## Objective

Expand Pillar 2 (Chip) coverage from the current 6 TWSE free-tier factors (max 50 pts)
to a richer, multi-source chip analysis layer. The user identified 6 categories of chip
data currently missing or only partially covered:

1. **法人籌碼** — Institutional flow (外資/投信/自營商 consecutive days for trust/dealer; 法人持股比率)
2. **融資融券** — Margin/short (融資使用率; 融券回補壓力 signal)
3. **借券賣出** — SBL / securities lending (借券賣出餘額, 占成交比重, 回補壓力)
4. **主力/分點籌碼** — Major player/branch chip (known FII branch detection; multi-day consecutive accumulation)
5. **大戶/散戶持股結構** — TDCC shareholder tier data (400張/1000張大戶比率 change; 股東人數)
6. **當沖與短線交易熱度** — Day-trade heat (當沖占比; 週轉率; abnormal volume flavor)

## Current State (post Round 2)

| Pillar | Factors | Max pts |
|--------|---------|---------|
| P1 Momentum | VWAP-5d, vol surge+direction, close strength, consec up, vol trend | 55 |
| P2 Chip (paid FinMind) | net_buyer_diff, concentration_top15, no_daytrade | 40 |
| P2 Chip (free TWSE) | foreign/trust/dealer net buy, margin change, all-inst, foreign consec | 50 |
| P3 Space | 20d high, 60d high, MA align, MA20 slope, RS vs TAIEX | 45 |
| Risk deductions | daytrade −25, short spike −10 | — |

**What's already in TWSEChipProxy:** foreign_net_buy, trust_net_buy, dealer_net_buy,
margin_balance_change, foreign_consecutive_buy_days, short_balance_increased, short_margin_ratio.

**What's already in ChipReport:** top_buyers (BrokerWithLabel with label/reversal_rate),
concentration_top15, net_buyer_count_diff (3-day), risk_flags, active_branch_count.

## Data Source Reality Check

| Category | Data Source | Availability |
|----------|------------|--------------|
| 法人持股比率 | MOPS/TWSE monthly | Complex, monthly lag → low signal value |
| 投信/自營商連買天數 | TWSE T86 lookback | Implementable (same pattern as foreign_consec) |
| 融資使用率 | TWSE MI_MARGN (融資限額 column) | Implementable (column already fetched) |
| 融券回補壓力 | Derived from short_balance + margin | Implementable (derived metric) |
| 借券賣出 | TWSE SBL endpoint (TWT93U / T264) | Implementable (new TWSE endpoint) |
| 主力分點連買 | FinMind paid 分點資料 | Paid tier only |
| 知名外資分點偵測 | Hardcoded FII branch code list | Implementable (hardcoded + ChipReport) |
| TDCC 大戶持股 | TDCC public API (weekly) | Implementable but weekly lag |
| 當沖占比 | TWSE daily disclosure (各股當沖交易) | Implementable (new TWSE endpoint) |
| 週轉率 | OHLCV volume / issued shares | Needs issued shares data (FinMind or TWSE) |

## Proposed Changes

### Tier A: High Value / Free Data (implement this sprint)

**A1 — 投信/自營商連買天數** (new free-tier scoring, T86 lookback)
- `trust_consecutive_buy_days` and `dealer_consecutive_buy_days` in TWSEChipProxy
- Score: `+5` if trust_consecutive >= 3; `+3` if dealer_consecutive >= 3
- Same lookback pattern as `_fetch_foreign_consecutive_days`
- New pts fields: `twse_trust_consec_pts`, `twse_dealer_consec_pts`

**A2 — 融資使用率** (derived from MI_MARGN)
- `margin_utilization_rate` = 融資餘額 / 融資限額
- Score: `+5` if utilization < 20% (healthy, room to buy); `-5` if utilization > 80% (crowded)
- New TWSE field: `margin_credit_limit` in MI_MARGN fetch
- New pts field: `twse_margin_utilization_pts`

**A3 — 融券回補壓力** (derived metric)
- `short_cover_pressure` = short_balance / avg_daily_volume (days-to-cover)
- Score as LLM hint (non-scoring) — days-to-cover > 3 → flag for LLM context
- Add to `_AnalysisHints`: `short_cover_days: float | None`

**A4 — 借券賣出占比** (new TWSE SBL endpoint)
- TWSE endpoint: `https://www.twse.com.tw/rwd/zh/shortselling/TWT93U`
- Fields: 借券賣出張數, 占成交量比重
- Score: `-5` if SBL ratio > 10% (heavy securities lending selling = bearish pressure)
- New pts field: `twse_sbl_deduction` (negative scoring)
- `sbl_ratio` in TWSEChipProxy

**A5 — 知名外資分點偵測** (hardcoded FII list, FinMind paid only)
- Hardcoded dict of known foreign institutional branch codes:
  `{"1480": "摩根大通", "1560": "美林", "9200": "瑞銀", ...}`
- Score: `+5` if any known FII in top_buyers (for paid tier only)
- Adds to `no_daytrade_pts` → split into separate `fii_presence_pts`

**A6 — 當沖占比** (TWSE daily 當沖 table)
- TWSE endpoint: `https://www.twse.com.tw/rwd/zh/block/TWTB4U` or similar
- Fields: 當沖買/賣張數, 占成交量比重
- Score: LLM hint (non-scoring) — high day-trade % indicates retail frenzy
- Add to `_AnalysisHints`: `daytrade_ratio: float | None`
- Also: if daytrade_ratio > 40%, reduce confidence in trend signals (flag)

### Tier B: Medium Value (next sprint — needs investigation)

**B1 — TDCC 大戶持股結構** (weekly data, TDCC public API)
- Weekly TDCC endpoint provides shareholder tier breakdown (>400 lots, >1000 lots)
- Requires: ticker → outstanding shares lookup
- Score: `+5` if 400張+ tier count increased week-over-week
- Deferred: requires new infrastructure (TDCC client + weekly schedule)

**B2 — 週轉率** (volume / outstanding shares)
- Requires issued shares lookup (FinMind `TaiwanStockInfo` or TWSE)
- Score: turnover in 1–5% range = healthy; >10% = potential blow-off
- Deferred: needs outstanding shares data source validation

### Not in scope

- 法人持股比率月報 — monthly lag makes it useless for daily signal
- 主力連買 (FinMind paid 分點 consecutive) — gated by Phase 1 (spike already validated, but paid API required)
- Real-time margin/short intraday — Phase 4

## Implementation Approach

All Tier A changes fit within existing architecture:
- `TWSEChipProxy` model: add new fields
- `ChipProxyFetcher`: add new `_fetch_*` methods
- `_ScoreBreakdown`: add new pts fields
- `TripleConfirmationEngine._compute()`: wire new scoring
- `_AnalysisHints`: add new hint fields

Score rebalancing needed after adding Tier A:
- Free-tier max will increase from 50 to ~68 pts (adds: 5+3+5+0-5+5 = 13 net)
- Need to recalibrate LONG threshold for free tier (currently 55)
- Proposal: keep LONG threshold at 55 but cap Pillar 2 free at 50 pts in scoring

## Premises

1. TWSE SBL endpoint (TWT93U) is public and parseable — VALIDATION REQUIRED (scripts/validate_sbl_endpoint.py) before A4 implementation
2. Trust/dealer consecutive day pattern mirrors foreign_consec — existing T86 lookback ALREADY fetches trust/dealer; refactor, don't duplicate
3. MI_MARGN contains 融資限額 column — VALIDATION REQUIRED (scripts/validate_margin_utilization.py) before A2 implementation
4. Known FII branch codes are stable enough to hardcode (change rate: maybe 1-2 per year) — CONFIRMED
5. Adding new TWSEChipProxy fields is non-breaking (Pydantic defaults to 0/None) — CONFIRMED
6. Score rebalancing: raise LONG_FREE 55 → 60 (no cap); document in README + CHANGELOG — APPROVED by user

## Test Requirements

- Unit tests for each new `_fetch_*` method (mock TWSE responses)
- Unit tests for each new scoring factor (mock TWSEChipProxy with new fields)
- Integration test: confirm SBL endpoint returns parseable data for 2330
- Regression: existing 159 unit tests must still pass

## Files in Blast Radius

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/models.py` | Add fields to TWSEChipProxy |
| `src/taiwan_stock_agent/infrastructure/twse_client.py` | Add _fetch_trust_consec, _fetch_dealer_consec, _fetch_sbl_data, _fetch_margin_utilization |
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | Add new pts fields to _ScoreBreakdown, wire _compute() |
| `src/taiwan_stock_agent/domain/broker_label_classifier.py` | Add FII branch code dict (A5) |
| `tests/unit/test_triple_confirmation_engine.py` | New factor tests |
| `tests/unit/test_twse_client.py` | New fetch method tests |
| `README.md` | Update factor table |

---

## Phase 1: CEO Review

### Pre-Review System Audit

- Branch: main (9 commits total, on main since project start)
- Hot files (last 30d): models.py, triple_confirmation_engine.py, twse_client.py — exactly the blast radius for this plan
- TODOs in blast radius: `src/taiwan_stock_agent/api/main.py` and `src/taiwan_stock_agent/domain/broker_label_classifier.py`
- No stash, no in-flight PRs
- Design doc: none for this branch (signal-engine-design.md is the overall product spec)

### Step 0A: Premise Challenge

**Premise 1: "TWSE SBL endpoint (TWT93U) is public and parseable"**
- STATUS: UNVERIFIED. The user's own experience shows T86/MI_MARGN works in some environments but not others (IP-block confirmed in this session). TWT93U is documented in TWSE public disclosure but schema stability is unknown.
- Severity: HIGH
- Fix: Add a `validate_sbl_endpoint.py` spike script before implementing A4. If it fails, A4 is deferred.

**Premise 2: "Trust/dealer consecutive mirrors foreign_consec pattern"**
- STATUS: MOSTLY VALID, but with a hidden efficiency gain. Reading `twse_client.py:374-402`, `_fetch_foreign_consecutive_days()` already calls `_fetch_t86_data()` which returns `(foreign, trust, dealer)` for EACH lookback date. Trust and dealer consecutive counts can be extracted FROM THE EXISTING LOOKBACK with zero new network calls. The function just discards the trust/dealer values.
- This is a significant implementation efficiency — not a premise risk, but a design opportunity.

**Premise 3: "MI_MARGN already returns 融資限額 column"**
- STATUS: NOT CONFIRMED BY CODE. `_fetch_margin_balance_one_day()` (twse_client.py:229-285) only parses `融資餘額` and `融券餘額`. `融資限額` is never parsed. Whether TWSE MI_MARGN includes this column requires a live API check.
- Severity: MEDIUM
- Fix: Validate with a one-line script before implementing A2.

**Premise 4: "Known FII branch codes are stable enough to hardcode"**
- STATUS: VALID. Major FII branch codes (摩根大通=1480, etc.) are TWSE-assigned and rarely change. Acceptable maintenance burden.
- Severity: LOW

**Premise 5: "Adding new TWSEChipProxy fields is non-breaking"**
- STATUS: VALID. Pydantic BaseModel with default values is backward-compatible.
- Severity: LOW

**Premise 6: "Score cap at 50 prevents threshold recalibration"**
- STATUS: WRONG DESIGN. If we cap P2 free at 50 pts and existing factors already score 40 pts (typical strong chip case: 15+10+5+5+5=40), the new trust_consec(5) + dealer_consec(3) + margin_util(5) = 13 pts are only useful when base chips are weak (< 37). This renders the new factors near-useless for exactly the cases we want to improve. The cap is the wrong mechanism.
- Severity: HIGH
- Fix: Don't cap. Instead, raise the free LONG threshold from 55 to 60 (reflecting that the max free chip is now 63 pts, not 50). Document the recalibration explicitly.

### Step 0B: Existing Code Leverage

| Sub-Problem | Existing Code | Reuse |
|-------------|--------------|-------|
| Trust consec days | `_fetch_foreign_consecutive_days()` lookback loop already fetches trust data | Extract trust column from existing 3-tuple — zero new network calls |
| Dealer consec days | Same as above | Same |
| Margin utilization | `_fetch_margin_balance_one_day()` fetches MI_MARGN | Extend to also parse 融資限額 (if column exists) |
| SBL ratio | None | New `_fetch_sbl_data()` method required |
| FII detection | `ChipReport.top_buyers[].branch_code` already available | Add hardcoded FII dict, lookup in `_apply_paid_chip()` |
| Daytrade ratio | None | New `_fetch_daytrade_ratio()` method OR derive hint from existing OHLCV volume data |
| Short cover days | `short_balance_increased`, `short_margin_ratio`, OHLCV volume | Computable from existing data (no new fetch) |

**What's already in flight:** `_fetch_foreign_consecutive_days()` at twse_client.py:374 iterates 15 calendar days, calls `_fetch_t86_data()` (which returns a 3-tuple), and uses only index 0 (foreign). Indexes 1 (trust) and 2 (dealer) are DISCARDED. Trust/dealer consecutive days are FREE.

### Step 0C: Dream State Mapping

```
CURRENT STATE                THIS PLAN                     12-MONTH IDEAL
─────────────────────────    ──────────────────────────    ──────────────────────────
P2 free: 6 factors, 50pts    P2 free: ~11 factors, 63pts   P2 paid: full 分點 classifier
TWSE T86+MI_MARGN only       + SBL ratio + FII detect       + TDCC weekly big-holder
IP-blocked in some envs      + trust/dealer consec          + real-time brokerage API
LLM hints: RSI/MACD/52w      + short cover pressure hint    + full narrative, all chips
Score: up to 150pts          Score: up to 158pts (P2+P3)    Broker labeling DB = moat
```

This plan moves in the right direction. It does NOT advance the broker labeling DB moat (Phase 4) — it improves the free-tier chip signal quality. Both are valid but shouldn't be confused.

### Step 0C-bis: Implementation Alternatives

```
APPROACH A: Minimal — Trust/Dealer Consec Only (zero new endpoints)
  Summary: Extract trust/dealer consecutive from existing T86 lookback (already fetched).
           Add short_cover_days hint (derived from existing short data + OHLCV volume).
  Effort:  S (3 files, ~60 LOC + tests)
  Risk:    Very Low — no new network calls, pure refactor of existing lookback
  Pros:    Provably zero regression risk; immediately testable; no IP-block exposure
  Cons:    Misses SBL, FII detect, margin utilization, daytrade ratio
  Reuses:  _fetch_foreign_consecutive_days() logic entirely

APPROACH B: Full Tier A (as planned, with SBL endpoint validation gate)
  Summary: All 5 Tier A categories plus SBL/daytrade (gated by endpoint validation spike).
           Raise LONG free threshold from 55 → 60.
  Effort:  M (7 files, ~350 LOC + tests)
  Risk:    Medium — SBL and 當沖 endpoints unvalidated; same IP-block risk as TWSE
  Pros:    Complete chip story; SBL is genuine new signal; FII detect has clear edge
  Cons:    May discover endpoints don't work; two new TWSE dependencies
  Reuses:  Existing T86 lookback, MI_MARGN fetch, ChipProxyFetcher pattern

APPROACH C: Tier A minus new TWSE endpoints (FII detect + consec + cap removal)
  Summary: Everything from B EXCEPT SBL and 當沖 ratio (no new endpoints).
           Add FII detection (paid tier only, no new fetch) + trust/dealer consec.
           Margin utilization added AFTER validating MI_MARGN schema.
           Raise threshold from 55 → 58.
  Effort:  S-M (5 files, ~200 LOC + tests)
  Risk:    Low — no new unvalidated endpoints; conservative threshold adjustment
  Pros:    All wins of B without the infra risk; TWSE dependency count stays at 2
  Cons:    SBL deferred until endpoint validated; daytrade ratio deferred
  Reuses:  All existing TWSE fetch patterns
```

**RECOMMENDATION (auto-decided, P1+P3): Approach B, but gates SBL and 当沖 on endpoint validation scripts first (P3 pragmatic). If validation fails → fall back to Approach C.**

### Step 0D: Mode — SELECTIVE EXPANSION (auto-decided, P3: feature enhancement on existing system)

Cherry-pick analysis (auto-decided per 6 principles):

| # | Proposal | Effort | Auto-Decision | Principle | Rationale |
|---|----------|--------|---------------|-----------|-----------|
| A1 | Trust/dealer consec days (extract from existing lookback) | XS | APPROVED | P1+P2 | Free; reuses existing data; additive signal |
| A2 | Margin utilization (MI_MARGN 融資限額) | S | APPROVED conditionally | P1+P3 | Validate schema first; degrade to no-op if column missing |
| A3 | Short cover pressure (derived, LLM hint only) | XS | APPROVED | P5 | Explicit hint; derived from existing data; non-scoring |
| A4 | SBL ratio (new TWSE TWT93U endpoint) | M | APPROVED + VALIDATION GATE | P1+P6 | Validate endpoint before implementing scoring; drop if IP-blocked |
| A5 | FII branch detection (hardcoded dict) | XS | APPROVED | P1+P4 | No new endpoints; dict is the simplest possible implementation |
| A6 | Daytrade ratio hint (new TWSE endpoint) | M | APPROVED as hint only | P5+P1 | Non-scoring; if endpoint unavailable, skip silently |
| B1 | TDCC big-holder structure (weekly) | L | DEFERRED to TODOS | P3 | Different infrastructure; weekly lag limits signal value |
| B2 | Turnover rate | M | DEFERRED to TODOS | P3 | Needs outstanding shares data source validation |
| — | 法人持股比率月報 | — | REJECTED | P4 | Monthly lag; CMoney already shows this; no edge |
| — | LLM prompt quality improvement | M | TASTE DECISION | — | Subagent raises: improving LLM reasoning may yield more impact than scoring |

**Taste Decision surfaced: LLM prompt quality** — Subagent raises that improving the prompt to better narrate existing factors may yield more signal value than adding scoring factors. This is plausible but orthogonal to this plan's goal. Surfaced at Final Gate.

### Step 0E: Temporal Interrogation

```
HOUR 1 (foundations — CC: ~5 min):
  - Refactor _fetch_foreign_consecutive_days() to return all 3 consec counts
    (or add a new _fetch_institution_consecutive_days() that returns dict)
  - Validate: does the existing T86 lookback already cache trust/dealer per date?
    YES — parquet cache at twse_t86_{ticker}_{date}.parquet already includes trust/dealer

HOUR 2-3 (core logic — CC: ~10 min):
  - Update TWSEChipProxy model (models.py): add trust_consec, dealer_consec,
    sbl_ratio, margin_utilization_rate, daytrade_ratio, short_cover_days
  - Update _ScoreBreakdown: add twse_trust_consec_pts, twse_dealer_consec_pts,
    twse_margin_util_pts, twse_sbl_deduction, twse_fii_pts
  - Ambiguity: _compute() calls _apply_free_chip() which takes TWSEChipProxy.
    New fields flow through naturally — no signature change needed.

HOUR 4-5 (integration — CC: ~15 min):
  - Wire new scoring in _apply_free_chip(): 3 new additive pts + SBL deduction
  - Wire FII detection in _apply_paid_chip(): new fii_presence_pts
  - Update _LONG_THRESHOLD_FREE: 55 → 60
  - Update logger.debug string at triple_confirmation_engine.py:364-373
    (currently hardcodes all field names — will need to add new ones)

HOUR 6+ (tests — CC: ~20 min):
  - Mock TWSE T86 responses already in test_twse_client.py — extend with trust/dealer columns
  - New scoring tests: test_trust_consec_scores_correctly(), test_sbl_deduction_applied(), etc.
  - Regression: existing 159 tests must pass
  - New threshold test: verify LONG_FREE=60 is applied when twse_proxy.is_available=True
```

### Step 0F: Mode = SELECTIVE EXPANSION (confirmed). Approach B (with validation gates).

### Dual Voices — CEO

**CODEX SAYS (CEO — strategy challenge):**
- Core risk: chasing breadth over signal quality without a pre/post hit-rate study
- Wrong problem? Plan optimizes factor count, not decision accuracy or PnL — no KPI guardrails
- Data latency gaps ignored (TDCC weekly mixed with daily signals)
- Sourcing assumptions shaky: TWT93U/TWTB4U can rate-limit or schema-shift; FII codes age
- Scoring inflation unresolved — "Cap Pillar 2 at 50" is hand-wavy
- Coverage bias: everything is flow; no cross-check with fundamentals or macro regime
- Six-month regret: building infra for low-quality endpoints, discovering signal lift is minimal

**CLAUDE SUBAGENT (CEO — strategic independence):**
- Adding factors without outcome-driven diagnosis is the central flaw
- SBL endpoint validity unverified — spike first
- Score cap hack breaks interpretability; should raise threshold instead
- Trust/dealer consec likely correlated with foreign consec (may not add real signal)
- This plan does not advance the broker labeling DB moat; explicitly acknowledge it's operational, not strategic

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                               Claude  Codex  Consensus
  ────────────────────────────────────── ─────── ─────── ─────────
  1. Premises valid?                       MIXED   MIXED  DISAGREE → taste
  2. Right problem to solve?               MOSTLY  NO     DISAGREE → taste
  3. Scope calibration correct?            YES     MAYBE  DISAGREE → taste
  4. Alternatives sufficiently explored?   NO      NO     CONFIRMED — missing
  5. Competitive/market risks covered?     YES     YES    CONFIRMED — ok
  6. 6-month trajectory sound?             YES     MAYBE  DISAGREE → taste
═══════════════════════════════════════════════════════════════
CONFIRMED = both agree. DISAGREE = models differ (→ taste decision).
```

**CEO Dual Voice Summary:** Both voices independently flag: (a) SBL endpoint must be validated before implementation, (b) score inflation handling is unresolved, (c) outcome data should inform factor selection. Codex is more aggressive on strategic questioning; Subagent is more code-concrete. The core tension: "build more factors first" vs "validate signal value before building."

### Section 1: Architecture Review

No new architectural components. All changes are additive to existing layers:

```
TWSEChipProxy (models.py)           NEW FIELDS
  + trust_consecutive_buy_days       ←── from T86 lookback
  + dealer_consecutive_buy_days      ←── from T86 lookback
  + sbl_ratio                        ←── from TWT93U (if available)
  + margin_utilization_rate          ←── from MI_MARGN (if available)
  + daytrade_ratio                   ←── from TWTB4U (if available)
  + short_cover_days                 ←── derived (no new fetch)

ChipProxyFetcher (twse_client.py)
  _fetch_institution_consecutive()   ←── refactor of existing foreign_consec
  _fetch_sbl_data()                  ←── NEW endpoint (TWT93U)
  _fetch_margin_utilization()        ←── EXTEND MI_MARGN parse
  _fetch_daytrade_data()             ←── NEW endpoint (TWTB4U)

_ScoreBreakdown (triple_confirmation_engine.py)
  + twse_trust_consec_pts            ←── +5 if trust_consec >= 3
  + twse_dealer_consec_pts           ←── +3 if dealer_consec >= 3
  + twse_margin_util_pts             ←── +5 if util < 20%, -5 if util > 80%
  + twse_sbl_deduction               ←── -5 if sbl_ratio > 10%
  + paid_fii_presence_pts            ←── +5 if known FII in top_buyers (paid only)
  [_AnalysisHints]
  + short_cover_days                 ←── derived hint
  + daytrade_ratio                   ←── hint from TWTB4U

_LONG_THRESHOLD_FREE: 55 → 60       ←── recalibration (P2 free max: 50 → 63)
```

Happy/nil/error/empty paths for new fetch methods follow existing pattern: return None on any failure, populate data_quality_flags, never raise. This pattern is proven and consistent.

Coupling: No new coupling. All new fields are on `TWSEChipProxy` (passed by value to engine). FII dict is module-level constant in `broker_label_classifier.py` or a new `constants.py`.

### Section 2: Error & Rescue Map

```
METHOD/CODEPATH                   | WHAT CAN GO WRONG             | EXCEPTION CLASS
──────────────────────────────────┼───────────────────────────────┼──────────────────
_fetch_sbl_data()                 | TWT93U IP-blocked             | ConnectionError
                                  | Schema changed (no 借券 col)  | ValueError
                                  | Ticker not found              | StopIteration
                                  | Rate limited (429)            | HTTPError
_fetch_daytrade_data()            | TWTB4U endpoint not found     | HTTPError 404
                                  | HTML response (IP-block)      | JSONDecodeError
_fetch_margin_utilization()       | 融資限額 column missing       | ValueError
                                  | Value = 0 (no margin allowed) | ZeroDivisionError
_fetch_institution_consecutive()  | T86 returns no trust column   | IndexError (safe: returns 0)
FII detection (paid tier)         | branch_code not in dict       | KeyError (safe: no-op)

EXCEPTION CLASS              | RESCUED? | RESCUE ACTION              | SIGNAL SEES
─────────────────────────────┼──────────┼────────────────────────────┼──────────────
ConnectionError              | Y        | Return None, flag TWSE_*   | 0 pts / no deduction
HTTPError (4xx/5xx)          | Y        | Return None, flag          | Same
JSONDecodeError              | Y        | Return None, flag          | Same
ValueError (schema)          | Y        | Return None, flag *_SCHEMA | Same
ZeroDivisionError (margin)   | Y        | Return None (skip scoring) | 0 pts
KeyError (FII lookup)        | Y        | No-op (not FII)            | 0 pts
```

All new fetch methods follow the existing `try/except Exception` → `return None` pattern. This is acceptable here because each method is a single isolated data source, and failure degrades to 0 pts (not a silent incorrect result). The existing codebase consistently uses this pattern.

### Section 3: Security & Threat Model

No new user inputs. No new endpoints. No new secrets. All data flows are read-only fetches from public TWSE endpoints. No PII. No auth tokens.

FII hardcoded dict: Not a security concern. Public TWSE branch code data.

Threshold recalibration (55 → 60): Not a security concern. But it IS a behavioral change that affects all users. The change must be documented in a migration note or CHANGELOG so users understand why signal counts shift.

### Section 4: Data Flow & Interaction Edge Cases

Trust/dealer consec computation path:
```
T86 lookback (existing) ──▶ _fetch_t86_data(date) ──▶ (foreign, trust, dealer)
    │                             │                          │
    ▼                             ▼                          ▼
  [cache hit?]              [ticker not found?]       [trust col missing?]
  [parquet exists]          [→ return (None,None,None)]  [→ trust = None, skip]
    │                                                      │
    ▼                                                      ▼
  [trust = None?] ──▶ skip this day (don't count)   [consec count stays 0]
  [trust > 0?]   ──▶ increment count
  [trust ≤ 0?]   ──▶ stop counting (streak broken)
```

SBL ratio path:
```
fetch TWT93U ──▶ parse 借券賣出 column ──▶ sbl_volume / total_volume = sbl_ratio
    │                    │
    ▼                    ▼
  [IP-block?]    [schema changed?]
  [→ None]       [→ None + flag]
    │
    ▼
  [sbl_ratio > 10%] → twse_sbl_deduction = -5
  [sbl_ratio ≤ 10%] → 0
  [sbl_ratio = None] → 0
```

Edge cases:
- Ticker not traded today: T86 returns no row → trust/dealer = None → consec = 0 (correct)
- Non-margin stock (ETF): 融資限額 = 0 → margin_utilization undefined → no scoring (correct)
- New listing: 50d history < 15 days → T86 lookback returns fewer days → consec = actual count (correct)

### Section 5: Observability & Deployment

The `logger.debug()` at `triple_confirmation_engine.py:364-373` explicitly lists every score field. Adding new fields without updating this string creates a monitoring gap — the log becomes misleading.

Required update:
```python
logger.debug(
    "score breakdown for %s: p1=%d+%d+%d+%d+%d p2_paid=%d+%d+%d+%d p2_free=%d+%d+%d+%d+%d+%d+%d+%d+%d "
    "p3=%d+%d+%d+%d+%d deduct=%d+%d+%d flags=%s → total=%d",
    ...
    # add: bd.paid_fii_presence_pts, bd.twse_trust_consec_pts, bd.twse_dealer_consec_pts,
    #      bd.twse_margin_util_pts, bd.twse_sbl_deduction
```

### Section 6: Failure Modes Registry

| # | Failure Mode | Likelihood | Impact | Mitigated? |
|---|-------------|------------|--------|------------|
| F1 | TWT93U IP-blocked (same issue as T86 in this env) | HIGH | SBL scoring silent zero | YES — is_available pattern |
| F2 | MI_MARGN has no 融資限額 column | MEDIUM | Margin util never scores | YES — column check before use |
| F3 | Trust/dealer consec ≥ 3 but correlated with foreign_consec (no additive signal) | MEDIUM | Score inflated; false confidence | PARTIALLY — need empirical check |
| F4 | LONG_FREE threshold change (55→60) confuses user (fewer LONGs signals temporarily) | HIGH | User behavior change | NO — needs changelog note |
| F5 | Debug log not updated — new fields invisible in logs | MEDIUM | Debugging harder | NO — explicit gap, must fix |

### CEO Completion Summary

| Category | Finding | Auto-Decision | Taste? |
|----------|---------|---------------|--------|
| Premise: SBL endpoint | UNVERIFIED — spike required | Gate on validation | No |
| Premise: MI_MARGN cols | UNVERIFIED — check schema | Conditional | No |
| Score cap hack | REJECT cap; raise threshold instead | Raise LONG_FREE to 60 | No |
| Trust/dealer consec | FREE from existing lookback — refactor | Approve | No |
| Threshold recalibration | Must document in README/CHANGELOG | Approved | No |
| LLM prompt vs scoring factors | Subagent: LLM prompt may yield more impact | Deferred to TODOS | TASTE |
| Outcome data validation | Both voices: run signals_outcomes analysis first | Recommend but not block | TASTE |

**NOT in scope (this plan):**
- Phase 4 broker labeling DB / collective label curation — different strategic work
- TDCC weekly shareholder structure (B1) — different infrastructure
- Real-time / intraday data — Phase 4
- Turnover rate (B2) — needs outstanding shares data source

**What already exists and WILL be reused:**
- `_fetch_foreign_consecutive_days()` → refactor to handle trust/dealer simultaneously
- `_fetch_margin_balance_one_day()` → extend to optionally parse 融資限額
- `ChipProxyFetcher.fetch()` pattern (try → flag → return None) → all new methods follow this
- `_apply_free_chip()` → extend in-place with new pts fields
- `_AnalysisHints` → extend with new hint fields

**Phase 1 complete.** Codex: 8 concerns. Claude subagent: 7 issues. Consensus: 2/6 confirmed, 4 disagreements (surfaced at gate). Passing to Phase 3 (Design skipped — no UI scope).

---

## Phase 3: Eng Review

### Scope Challenge

Plan touches 7 files. Max ~350 LOC. No new classes/services beyond a refactored helper in `twse_client.py`. This is well within HOLD SCOPE territory — complexity check passes.

Minimum set:
- Trust/dealer consec: requires models.py + twse_client.py (refactor 1 method) + engine._apply_free_chip()
- FII detect: requires engine._apply_paid_chip() + a dict (3-5 lines)
- SBL ratio: requires models.py + new _fetch_sbl_data() + engine._apply_free_chip()
- Threshold: requires 1 constant change

Deferred without blocking: margin utilization (pending schema check), daytrade ratio (pending endpoint check), TDCC (different infra).

### Architecture ASCII Diagram (new components)

```
┌─────────────────────────────────────────────────────────────────┐
│  ChipProxyFetcher.fetch()                                        │
│                                                                  │
│  Existing calls:                                                 │
│    _fetch_t86_data()          → foreign, trust, dealer          │
│    _fetch_margin_balance_*()  → margin_balance                  │
│    _fetch_short_balance_*()   → short_balance                   │
│    _fetch_foreign_consecutive_days() → foreign_consec [REFACTOR]│
│                                                                  │
│  NEW/REFACTORED:                                                 │
│    _fetch_institution_consecutive_days() [REFACTOR]             │
│      ↳ reuses T86 lookback; returns (foreign_c, trust_c, dealer_c)│
│    _fetch_sbl_data() [NEW — gated by endpoint validation]       │
│      ↳ TWSE TWT93U → sbl_ratio                                  │
│    _fetch_margin_utilization() [NEW — gated by schema check]    │
│      ↳ MI_MARGN 融資限額 column → margin_utilization_rate       │
│    _fetch_daytrade_data() [NEW — LLM hint only]                  │
│      ↳ TWSE TWTB4U → daytrade_ratio                             │
│                                                                  │
│  Returns TWSEChipProxy with 6 new fields                        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  TripleConfirmationEngine._apply_free_chip()                    │
│                                                                  │
│  NEW scoring:                                                    │
│    twse_trust_consec_pts  = +5 if trust_consec >= 3             │
│    twse_dealer_consec_pts = +3 if dealer_consec >= 3            │
│    twse_margin_util_pts   = +5 if util < 20% / -5 if util > 80%│
│    twse_sbl_deduction     = -5 if sbl_ratio > 10%              │
│                                                                  │
│  NEW in _apply_paid_chip():                                     │
│    paid_fii_presence_pts  = +5 if known FII in top_buyers       │
│                                                                  │
│  _LONG_THRESHOLD_FREE: 55 → 60                                  │
└─────────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  _AnalysisHints (non-scoring, LLM context)                      │
│    + short_cover_days  (derived: short_balance / avg_volume)    │
│    + daytrade_ratio    (from TWTB4U if available)               │
└─────────────────────────────────────────────────────────────────┘
```

### Test Coverage Diagram

| New Codepath | Test Type | Exists? | Required Test |
|-------------|-----------|---------|---------------|
| trust_consec from T86 lookback | Unit | NO | `test_trust_consecutive_days_from_t86_lookback()` |
| dealer_consec from T86 lookback | Unit | NO | `test_dealer_consecutive_days_from_t86_lookback()` |
| trust_consec_pts scoring (+5 if ≥3) | Unit | NO | `test_trust_consec_scores_five_when_three_or_more()` |
| dealer_consec_pts scoring (+3 if ≥3) | Unit | NO | Same pattern |
| sbl_ratio parse from TWT93U | Unit | NO | `test_fetch_sbl_data_parses_correctly()` (mock HTTP) |
| sbl_deduction (-5 when >10%) | Unit | NO | `test_sbl_deduction_applied_when_ratio_exceeds_threshold()` |
| margin_utilization parse | Unit | NO | `test_fetch_margin_utilization_parses_credit_limit()` |
| margin_util_pts scoring | Unit | NO | `test_margin_utilization_score_healthy_crowded()` |
| FII detection in top_buyers | Unit | NO | `test_paid_chip_fii_detection_awards_five_points()` |
| daytrade_ratio hint | Unit | NO | `test_daytrade_ratio_hint_populated()` |
| short_cover_days hint | Unit | NO | `test_short_cover_days_derived_from_existing_data()` |
| LONG_FREE threshold = 60 | Unit | NO | `test_long_threshold_free_is_sixty()` |
| T86 endpoint unavailable → consec = 0 | Unit | PARTIAL (existing) | Extend existing mock |
| SBL unavailable → no deduction | Unit | NO | `test_sbl_unavailable_no_deduction()` |
| Existing 159 tests still pass | Regression | YES | No change required |

**All 14 new tests + regression:** ~200 lines. Standard mock pattern (mock TWSE HTTP response with real-looking JSON). (human: 1 day / CC+gstack: 15 min)

### Section: Performance

Trust/dealer consec: Zero new network calls — cached T86 data already fetched. No performance impact.

SBL fetch: 1 new TWSE request per ticker per day. Cache pattern is identical to T86/MI_MARGN (parquet file). After first fetch, subsequent runs are free (cache hit). For a 5-ticker watchlist: 5 additional requests on first run only.

The `_fetch_foreign_consecutive_days()` loop already makes up to 7 T86 requests per ticker. These are cached. Refactoring to also extract trust/dealer adds zero requests.

### Section: DRY Violations

Current code: `_fetch_foreign_consecutive_days()` has its own loop logic. The refactored version should return all 3 consecutive counts in one pass. If instead we add `_fetch_trust_consecutive_days()` as a separate method duplicating the loop, that's a DRY violation. Must use a single `_fetch_institution_consecutive_days()` returning a dict or tuple.

### Failure Modes Registry (Eng perspective)

| # | Failure Mode | Detected By | Mitigated? |
|---|-------------|-------------|------------|
| F6 | LONG_FREE threshold raised to 60 but tests still assert 55 | Test run | NO — update test assertions |
| F7 | _ScoreBreakdown.total property not updated to include new pts fields | Test: score = 0 when it should be > 0 | CRITICAL — must update total property |
| F8 | logger.debug format string doesn't include new fields | Manual inspection | LOW severity but confusing |
| F9 | _apply_paid_chip() FII dict import creates circular import | Import error at startup | Check: dict should be in constants.py or local |
| F10 | SBL cache key collision with existing cache keys | Data corruption | Verify: use `twse_sbl_{ticker}_{date}.parquet` (unique prefix) |

**Critical gap:** `_ScoreBreakdown.total` at triple_confirmation_engine.py:166-191 is an explicit sum of all pts fields. If new fields like `twse_trust_consec_pts` and `twse_dealer_consec_pts` are added to the dataclass but NOT added to `total`, they score 0 in all cases. This is the most likely implementation error — easy to miss.

### Eng Completion Summary

| Category | Finding | Status |
|----------|---------|--------|
| DRY | Consec days must be unified into one method | Mandatory |
| _ScoreBreakdown.total | Must include all new pts fields | CRITICAL |
| Debug log update | Must reflect new fields | Required |
| New tests: 14 cases | All new codepaths covered | Required |
| Threshold recalibration | LONG_FREE 55 → 60 + docs | Required |
| SBL endpoint validation script | Before implementing A4 | Pre-requisite |
| MI_MARGN schema validation | Before implementing A2 | Pre-requisite |

**Phase 3 complete.** 2 critical gaps (total property, DRY violation risk). 14 new unit tests required. Test plan written below.

---

## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|
| 1 | CEO | SELECTIVE EXPANSION mode | P3 | Feature enhancement on existing system | SCOPE EXPANSION |
| 2 | CEO | Approach B with validation gates | P1+P3 | Complete but pragmatic — gate risky endpoints | Approach A (too minimal), C (same but rename) |
| 3 | CEO | Reject score cap at 50; raise threshold to 60 | P5 | Cap renders new factors useless for strong-chip stocks | Cap at 50 |
| 4 | CEO | Trust/dealer consec: refactor existing loop | P4 | Zero new network calls; existing code already fetches the data | New methods |
| 5 | CEO | FII detect: approved, XS effort | P1+P2 | No new endpoints; dict lookup | Deferred |
| 6 | CEO | TDCC big-holder: deferred | P3 | Different infra; weekly lag limits value | Approved |
| 7 | CEO | Outcome data validation: TASTE (surfaced at gate) | — | Both voices recommend; but is a separate analysis task | Gate block |
| 8 | CEO | LLM prompt improvement: TASTE (deferred to TODOS) | P3 | Valid alternative; orthogonal to this plan | Included here |
| 9 | Eng | _ScoreBreakdown.total: must include new fields | P1 | Critical: omission = silent zero scoring | N/A |
| 10 | Eng | SBL/margin schema validation scripts: pre-requisites | P6 | Prevents building fetchers for unavailable data | Skip validation |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open | SBL unvalidated, score cap rejected, 2 taste decisions |
| Codex Review | CEO dual voice | Independent 2nd opinion | 1 | issues_found | Factor breadth without KPIs; endpoint stability |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | issues_open | 2 critical gaps (total property, DRY); 14 new tests |
| Design Review | skipped | No UI scope | 0 | N/A | — |

**CEO Voices:** Codex 8 concerns / Subagent 7 issues / Consensus 2/6 confirmed, 4 disagreements
**Eng Voices:** To be run at implementation time

**VERDICT:** REVIEWED — 2 taste decisions surfaced at gate. 2 critical eng gaps documented. Plan is implementation-ready pending user approval.
