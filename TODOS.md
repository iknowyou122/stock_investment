# TODOS

## P1 — Must resolve before Phase 3b

### Verify FinMind commercial licensing
**What:** Read FinMind TOS and confirm whether commercial data resale is permitted.
**Why:** Phase 3b sells a Broker Label API whose value derives from FinMind's `TaiwanStockBrokerTradingStatement` data. If resale is prohibited, Phase 3b requires a licensed data source (TWSE direct feed or equivalent) instead.
**Pros:** Prevents legal exposure; 1-hour task with high risk-mitigation value.
**Cons:** None — no engineering cost.
**Context:** FinMind is the sole data source for 分點 broker data. Their free tier is permissive but commercial redistribution terms are unverified. Must resolve before Phase 3b development starts, not on launch day.
**Effort:** S (human: 1-2 hours / CC: not applicable — requires human legal read)
**Priority:** P1
**Depends on:** Nothing — resolve immediately.

### Regulatory check: FSC investment advisory scope
**What:** Brief legal consultation on whether a broker signal API requires FSC registration as an investment advisor.
**Why:** Taiwan FSC has been expanding robo-advisory regulation. A "broker behavior signal API" sold commercially may fall under investment advisory regulation. Low cost to check, high risk if skipped.
**Pros:** Prevents regulatory surprise at Phase 3b launch.
**Cons:** Costs a legal consultation fee (typically NT$5,000-10,000 for a brief opinion).
**Context:** Identified in CEO review as a Phase 3b pre-work item. Must resolve before public announcement of Phase 3b API.
**Effort:** S (human: 1-2 hours / CC: not applicable)
**Priority:** P1
**Depends on:** Nothing.

---

## P2 — Phase 4 / future work

### Broker label time-decay (prevent label fossilization)
**What:** Add a recency-weighted reversal_rate calculation: weight recent trades more than old ones (e.g., exponential decay with 6-month half-life).
**Why:** Broker branch behavior changes over time — mergers, strategy shifts, personnel changes. A lifetime reversal_rate from 2022-2024 may be stale by 2026. Labels will be fossilized if not updated.
**Pros:** More accurate labels; prevents false confidence in outdated patterns.
**Cons:** Adds complexity to Phase 1 backtest; requires more historical data to be meaningful.
**Context:** Codex flagged this. The collective curation (Phase 4) partially addresses this via community-sourced outcome updates, but a time-decay mechanism should be built into the initial classifier.
**Effort:** M (human: 2 days / CC+gstack: ~30 min)
**Priority:** P2
**Depends on:** Phase 1 broker label DB.

### net_buyer_count_diff volume weighting
**What:** Add a volume-weighted variant alongside the branch-count metric. `volume_net_diff = sum(buy_volume) - sum(sell_volume)` over last 3 days, normalized by ADV (average daily volume).
**Why:** A single small buyer branch can offset large sell volume in the branch-count metric. Volume weighting captures economic significance.
**Pros:** More accurate chip concentration signal.
**Cons:** Adds a second metric to calibrate; may need different threshold.
**Context:** Codex flagged that branch-count only is asymmetric. Phase 2 validation will reveal whether this matters in practice.
**Effort:** S (human: 2 hours / CC+gstack: ~15 min)
**Priority:** P2
**Depends on:** Phase 2 rule engine validation.


### VolumeProfile / POC redesign (Pillar 3)
**What:** Implement real VolumeProfile (Point of Control) using intraday tick data for Pillar 3 of Triple Confirmation.
**Why:** Phase 1-3 replaces Pillar 3 with a daily-close proxy (`close > N-day high`). Real POC requires intraday price-volume distribution, which requires tick data not available until Phase 4.
**Pros:** Unlocks true support/resistance analysis; the original design vision.
**Cons:** Requires intraday data feed (Phase 4) — significant infrastructure addition.
**Context:** Codex identified that assigning daily volume to the close price produces a meaningless resistance map. Phase 1-3 uses `close > 20-day high` (+20 pts) as a proxy. Phase 4 should replace this with real VolumeProfile from intraday data.
**Effort:** L (human: 1-2 weeks / CC+gstack: ~2 hours)
**Priority:** P2
**Depends on:** Phase 4 intraday data feed.

### CMoney 理財寶 marketplace integration
**What:** List the signal tool on CMoney's 理財寶 marketplace as a strategy provider.
**Why:** CMoney has 10M+ MAU and 2.4M+ paid subscribers. Ready-made distribution for signal tools. Could be a significant revenue channel.
**Pros:** Massive distribution; NT$revenue share from CMoney subscriber base.
**Cons:** Requires established track record and validated signal quality first. CMoney BD terms unknown.
**Context:** Deferred in CEO review until Phase 3 signal track record is established. Needs research on CMoney's open API program and rev-share terms.
**Effort:** M (human: 1-2 months BD / CC: ~1 hour for technical integration)
**Priority:** P2
**Depends on:** Phase 2 signal track record (30 days of logged signals, >55% win rate at confidence ≥70).

### LINE push notifications
**What:** Push daily signals to subscribers via LINE messaging API.
**Why:** NT$1,000-10,000/month per subscriber is the dominant Taiwan trader subscription format. High revenue potential.
**Pros:** LINE is how Taiwan traders consume signal content; word-of-mouth in LINE groups is organic distribution.
**Cons:** LINE API has rate limits and costs; requires subscriber management; support burden from daily signal delivery.
**Context:** Deferred in CEO review. Phase 3+ feature after signal quality is validated. LINE groups are also a distribution channel for the sector chip heat map (shared as text, not push).
**Effort:** M (human: 1 week / CC+gstack: ~1 hour)
**Priority:** P2
**Depends on:** Phase 3a CLI + validated signal track record.

---

## P3 — Pre-phase spike / engineering definitions

### Define VolumeProfile bucketing resolution (Pre-Phase-1 blocker)
**What:** Define the bucketing resolution for Phase 4 VolumeProfile: suggested 1% price buckets, node = bucket with >5% of 20-session cumulative volume.
**Why:** Without a defined bucketing resolution, two engineers will implement different algorithms. Must be locked in before Phase 2 (when real VolumeProfile replaces the proxy).
**Context:** From design doc reviewer concerns. Not blocking Phase 1 or the validation spike — blocking Phase 4 VolumeProfile implementation.
**Effort:** S (human: 1 hour / CC: 10 min)
**Priority:** P3
**Depends on:** Phase 4 intraday data decision.

### concentration_top15 thinly-traded edge case
**What:** Define behavior when `active_branches < 10`: skip concentration check and add `active_branch_count` to ChipReport.
**Why:** For thinly-traded stocks, fewer than 10 active broker branches may trade. Concentration metric becomes unreliable and misleading.
**Context:** From design doc reviewer concerns. Implement in ChipDetectiveAgent Phase 2.
**Effort:** S (human: 1-2 hours / CC: 10 min)
**Priority:** P3
**Depends on:** Phase 2 ChipDetectiveAgent implementation.

### Data alignment verification
**What:** Before running the validation spike, confirm FinMind 分點 settlement dates align with OHLCV trade dates using 2330 on a known date.
**Why:** If FinMind's broker data and price data use different date conventions (trade date vs settlement date), all join queries produce wrong results.
**Context:** Step zero of the pre-phase spike. Must verify before writing any analysis code.
**Effort:** S (human: 30 min / CC: 5 min)
**Priority:** P3
**Depends on:** Nothing — first task before spike.

---

## Chip Expansion Plan — Deferred Items (from /autoplan 2026-03-27)

### TDCC Big-Holder Structure (B1)
**What:** Fetch weekly TDCC shareholder tier data (400張/1000張大戶比率 change; 股東人數).
**Why:** Deferred from chip-factors-expansion-plan.md (Tier B). Requires new TDCC client + weekly schedule.
**Effort:** M (human: 2-3 days / CC: ~30 min implementation)
**Priority:** P3 — after Tier A is validated and signal lift confirmed.

### Turnover Rate (B2)
**What:** Compute 週轉率 = daily volume / outstanding shares (float).
**Why:** Deferred from chip-factors-expansion-plan.md (Tier B). Needs outstanding shares data source validation.
**Effort:** S (human: 4 hours / CC: ~15 min) — after data source confirmed.
**Priority:** P3 — after B1 or if outstanding shares source found.

### Outcome-Driven Factor Validation
**What:** Before adding chip factors, pull signal_outcomes table and analyze failure patterns.
  - For each CAUTION signal that should have been LONG: which factors were 0?
  - For each LONG signal that failed (T+2d loss): which factors were misleadingly high?
**Why:** Both CEO dual voices recommend this. Prevents building factors that don't fix real failures.
**Effort:** S (human: 2 hours / CC: ~15 min) — needs accumulated signal_outcomes data.
**Priority:** P2 — do this when signal_outcomes has ≥30 entries.

### LLM Prompt Quality Improvement
**What:** Improve StrategistAgent._format_hints_for_prompt() to better narrate existing chip factors.
**Why:** Claude subagent suggests this may yield more interpretive quality than adding scoring factors.
**Effort:** S (human: 2 hours / CC: ~15 min)
**Priority:** P2 — can run in parallel with chip factor implementation.

### SBL Endpoint Validation
**What:** Write scripts/validate_sbl_endpoint.py to test TWT93U endpoint availability and parse structure.
**Why:** Pre-requisite for A4 (SBL ratio scoring). Same IP-block issue as T86/MI_MARGN affects this.
**Effort:** XS (human: 30 min / CC: ~5 min)
**Priority:** P1 within chip expansion — must do BEFORE implementing _fetch_sbl_data().
