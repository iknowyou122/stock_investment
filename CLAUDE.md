## Project Context — Read First

Before doing any work in this repo, read these files in order:

1. `docs/design/signal-engine-design.md` — full technical spec: architecture decisions,
   Triple Confirmation formula, FinMind data constraints, broker label classifier logic,
   phase gates, and backtest success criteria. This is the source of truth for WHY
   the code is structured the way it is.
2. `docs/design/ceo-plan.md` — product vision, scope decisions, what was accepted vs
   deferred, and the 12-month ideal state. Read before proposing scope changes.
3. `DESIGN.md` — UI/visual design system for the Phase 3b landing page.
   Read before touching any frontend code.

If you skip these and make architectural decisions that contradict the design doc,
you will create drift that is expensive to fix.

## Phase Gates

| Phase | Status | Gate condition |
|-------|--------|----------------|
| Pre-spike | ✅ Done | `data_alignment_check.py` + `spike_validate.py` written |
| Phase 1 | ✅ Done | Broker label classifier + batch classifier + outcome recorder built |
| Phase 2 | ✅ Done | Triple Confirmation Engine ✅ · ScoutAgent ✅ · Round 2 deepening ✅ · Signal track record ✅ · Sector heat map ✅ |
| Phase 3a | ✅ Done | StrategistAgent CLI + LLM reasoning (Claude API) + TWSE free-tier proxy |
| Phase 3b | ✅ Done | FastAPI + auth + rate limiting ✅ · Real DB routes ✅ · /track-record ✅ · signal_outcomes table ✅ · /register endpoint ✅ |
| Phase 4 | ⏳ Not started | Collective label curation (outcome submission + Bayesian update) |

**Phase 4 remaining work (next):**
- Collective label curation: outcome submission + Bayesian update of reversal_rate
- Payment integration: Stripe/台灣Pay before issuing pro keys (stub in /v1/register)

Do not implement Phase N+1 without the Phase N gate condition being met.

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
- /office-hours
- /plan-ceo-review
- /plan-eng-review
- /plan-design-review
- /design-consultation
- /review
- /ship
- /land-and-deploy
- /canary
- /benchmark
- /browse
- /qa
- /qa-only
- /design-review
- /setup-browser-cookies
- /setup-deploy
- /retro
- /investigate
- /document-release
- /codex
- /cso
- /careful
- /freeze
- /guard
- /unfreeze
- /gstack-upgrade

If gstack skills aren't working, run `cd .claude/skills/gstack && ./setup` to build the binary and register skills.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.
