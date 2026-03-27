# Phase 4 Plan: Collective Label Curation

<!-- /autoplan restore point: /Users/07683.howard.huang/.gstack/projects/iknowyou122-stock_investment/main-autoplan-restore-20260327-161540.md -->

**Branch:** main | **Date:** 2026-03-27 | **Status:** Approved — Premise gate passed 2026-03-27

## Problem Statement

Phase 4 completes the data flywheel loop that makes the system defensible.

Right now, broker labels (`reversal_rate`) are static — computed once from historical FinMind data and never updated. But FinMind's 分點 data is paywalled, so free-tier users provide no feedback signal, and labels slowly decay as broker behavior shifts.

The fix: let the community submit signal outcomes. Each submission becomes a Bayesian observation that updates the `reversal_rate` for the branches that drove the signal. Over time, labels improve as more users contribute — the system used by 1,000 traders is more accurate than the one used by 10. That's the moat.

Phase 4 also adds a payment gate before pro API keys are issued.

## Scope

### In scope

**A. Outcome submission endpoint**
- `POST /v1/signals/{signal_id}/outcome` — anonymous submission (no PII stored)
- Payload: `did_buy: bool`, `outcome: "win" | "lose" | "break_even" | null`
- Rate-limited per API key (10 submissions/day free, 100/day pro)
- Records to new `community_outcomes` table
- Triggers async Bayesian update for contributing broker branches

**B. Bayesian reversal_rate updater**
- New domain class: `BayesianLabelUpdater`
- Beta-Bernoulli conjugate update: prior = (reversal_rate × sample_count, sample_count × (1 - reversal_rate)), likelihood = community reports
- New columns on `broker_labels`: `community_win_count INT DEFAULT 0`, `community_sample_count INT DEFAULT 0`, `community_signal_win_rate FLOAT DEFAULT NULL`
- `community_signal_win_rate` is SEPARATE from `reversal_rate` — community reports "did this signal make money?" ≠ D+2 reversal (different measurements, keep separate)
- `BayesianLabelUpdater.update(branch_code)` recomputes `community_signal_win_rate` after each batch of community reports
- Script: `scripts/run_bayesian_update.py` — runs as cron daily

**C. DB migration**
- `004_community_outcomes.sql` — new `community_outcomes` table
- `005_broker_labels_bayesian.sql` — add Bayesian columns to `broker_labels`

**D. Payment stub**
- `/v1/register` with `tier=pro` → returns a Stripe checkout session URL (not yet integrated — returns a placeholder URL with TODO comment)
- Free tier: unchanged (issue key immediately)
- New env var: `STRIPE_SECRET_KEY` (optional; if unset, pro registration returns a stub response)

### Not in scope

- Real Stripe webhook handling (requires production Stripe account + deployment)
- 台灣Pay integration (deferred, needs Taiwan-specific payment processor research)
- Community reputation scoring (who's trustworthy) — Phase 5
- Spam/bot filtering beyond rate limiting — Phase 5
- Real-time label updates (batch update via cron is sufficient at Phase 4 scale)

## Data Model

### New table: `community_outcomes`

```sql
CREATE TABLE community_outcomes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id       UUID NOT NULL REFERENCES signal_outcomes(signal_id) ON DELETE CASCADE,
    api_key_hash    VARCHAR(64) NOT NULL,  -- SHA256(api_key) — no raw key stored
    did_buy         BOOLEAN NOT NULL,
    outcome         VARCHAR(20),           -- 'win' | 'lose' | 'break_even' | NULL
    submitted_at    TIMESTAMP DEFAULT NOW(),
    -- Denormalized for update efficiency
    branch_codes    TEXT[],                -- which broker branches contributed to this signal
    ticker          VARCHAR(10) NOT NULL,
    signal_date     DATE NOT NULL
);
-- Prevent duplicate submissions per (api_key_hash, signal_id)
CREATE UNIQUE INDEX idx_community_outcomes_dedup
    ON community_outcomes (api_key_hash, signal_id);
```

### Updates to `broker_labels`

```sql
ALTER TABLE broker_labels
    ADD COLUMN community_win_count    INT NOT NULL DEFAULT 0,
    ADD COLUMN community_sample_count INT NOT NULL DEFAULT 0,
    ADD COLUMN community_signal_win_rate FLOAT;
-- community_signal_win_rate = posterior_wins / posterior_total (Beta-Bernoulli)
-- NOTE: this is NOT reversal_rate. reversal_rate = D+2 price reversal (historical).
--       community_signal_win_rate = trader-reported "did this signal make money?"
-- NULL = no community data yet
```

## Bayesian Update Formula

Prior: assume 50% win rate with weight equal to `community_sample_count` already accumulated
(cold-start: uniform Beta(1,1) — no prior assumptions before any data).

```
# After accumulating N community reports:
posterior_wins  = community_win_count
posterior_total = community_sample_count

community_signal_win_rate = (posterior_wins + 1) / (posterior_total + 2)
# Laplace smoothing (+1/+2) avoids 0% or 100% with few reports
```

NOTE: `reversal_rate` (D+2 historical reversal computed from FinMind 分點 data) is never
modified. `community_signal_win_rate` is a completely separate metric. Both can be
surfaced to users; they measure different things.

This is a Beta-Bernoulli conjugate update. Prior = Beta(1,1) (uniform). Each "win"
community report increments `community_win_count`; each report (win/lose/break_even)
increments `community_sample_count`. `break_even` counts as total sample but NOT a win.

**`outcome: null` semantics (explicit):** `null` means "trade not yet settled — bought but
haven't checked result yet." The submission is recorded to `community_outcomes` but
`community_win_count` and `community_sample_count` are NOT updated until outcome is resolved.
The daily cron only aggregates `WHERE outcome IS NOT NULL`.

## New Files

```
src/taiwan_stock_agent/
  domain/
    bayesian_label_updater.py    # BayesianLabelUpdater class
  api/
    main.py                      # +POST /v1/signals/{signal_id}/outcome
    schemas.py                   # +OutcomeRequest, +OutcomeResponse, +RegisterResponse update

db/migrations/
  004_signal_outcomes_branches.sql  # ADD COLUMN branch_codes TEXT[] to signal_outcomes
  005_community_outcomes.sql
  006_broker_labels_bayesian.sql

scripts/
  run_bayesian_update.py         # cron: daily Bayesian update pass

tests/unit/
  test_bayesian_label_updater.py # unit tests for Bayesian math + updater
  test_api_outcome_endpoint.py   # unit tests for POST /outcome
```

## Engineering Blockers (resolved in plan, implement before coding)

From dual-voice eng review (2026-03-27):

**1. `branch_codes` write path (BLOCKER — fixed in plan above)**
- `signal_outcomes` table did NOT store broker branches — ephemeral signal data was dropped after generation
- Fix: Migration 004 adds `branch_codes TEXT[]` to `signal_outcomes`
- `SignalOutcomeRepository.record()` must accept and store branch_codes from signal.top_brokers
- Outcome endpoint reads `signal_outcomes.branch_codes` → denormalizes into `community_outcomes.branch_codes`

**2. Rate limiting (CONCERN — addressed in implementation)**
- Existing rate limiter is monthly + in-memory; Phase 4 needs daily + per-endpoint
- Implementation note: add `outcome_submissions_today` column to `api_keys` table with daily reset
- Free: 10/day, Pro: 100/day (separate quota from existing signal scan limits)

**3. Payment stub URL (CONCERN — fixed in API design below)**
- Plan originally specified `https://buy.stripe.com/placeholder` — real Stripe domain, looks misleading
- Fixed: use `https://checkout.example.com/stub?session=TODO_STRIPE_PHASE5` (no real domain)

**4. `outcome: null` semantics (CONCERN — documented above, explicit)**

## API Design

### POST /v1/signals/{signal_id}/outcome

**Request:**
```json
{
  "did_buy": true,
  "outcome": "win"
}
```

**Response 200:**
```json
{
  "message": "Outcome recorded. Thank you for contributing.",
  "signal_id": "uuid",
  "community_count": 42
}
```

**Errors:**
- 404: signal_id not found
- 409: duplicate submission (same API key + same signal_id)
- 422: invalid outcome value
- 429: rate limit exceeded

### POST /v1/register (updated)

**Free tier:** unchanged — issue key immediately.

**Pro tier (stub):**
```json
{
  "api_key": null,
  "tier": "pro",
  "message": "Payment required. Complete checkout to activate your pro key.",
  "checkout_url": "https://checkout.example.com/stub?session=TODO_STRIPE_PHASE5",
  "payment_status": "pending"
}
```

## Success Criteria (Phase 4 gate)

- `POST /v1/signals/{signal_id}/outcome` endpoint functional (201 on valid, 409 on duplicate)
- Bayesian update formula unit-tested (correct posterior with known prior + 5 community reports)
- DB migrations 004, 005, 006 run without errors on clean schema
- `signal_outcomes.branch_codes` populated when signals are recorded
- `/v1/register?tier=pro` returns stub checkout URL (not crashes, not real Stripe domain)
- All 179 existing unit tests still pass
- New unit tests: ≥ 20 covering Bayesian math, API endpoint, dedup, rate limiting

## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|
| 1 | CEO | `community_signal_win_rate` is separate from `reversal_rate` | Domain integrity | Community "win/lose" ≠ D+2 reversal — different measurements, merging would corrupt historical labels | Merge into `reversal_rate` |
| 2 | CEO | Build infrastructure now, before first user | Flywheel readiness | Zero-user start is fine — table exists, cron runs, data accumulates organically | Wait for user traction |
| 3 | CEO | Anonymous submissions (SHA256 api_key_hash) | Privacy-first | No PII stored. Dedup still works via hash. | Store user IDs |
| 4 | CEO | Stub payment gate (placeholder URL) | Scope control | Real Stripe webhook = production account + deployment infra. Defer to Phase 5. | Full Stripe integration now |
| 5 | Eng | Add migration 004 for `signal_outcomes.branch_codes` | Data integrity | Broker branch info was dropped after signal generation — must persist it to enable Bayesian updates | Reconstruct at query time (too expensive) |
| 6 | Eng | `outcome: null` = accepted but excluded from Bayesian counts | Correctness | Unresolved observations shouldn't influence posterior; cron uses `WHERE outcome IS NOT NULL` | Count null as 0.5 sample |
| 7 | Eng | Use `checkout.example.com/stub` not `buy.stripe.com` | Security | Real Stripe domain URL is misleading; could redirect real traffic accidentally | Real Stripe placeholder |
| 8 | Eng | Per-endpoint daily rate limit (new col on api_keys) | Abuse prevention | Existing monthly rate limit insufficient for daily 10/100 quota per endpoint | Shared quota across endpoints |
