-- Migration 005: Community outcome submissions for collective label curation.
--
-- Each row represents one user's self-reported outcome for a signal they
-- acted on. These are the raw inputs to the Bayesian win-rate updater.
--
-- Design decisions:
--   - api_key_hash: SHA-256 of the submitting user's API key. The raw key
--     is never stored here; the hash is sufficient for deduplication.
--   - outcome NULL: allowed — user can report "did_buy" but defer the outcome
--     until the trade settles. The Bayesian updater excludes NULL outcomes.
--   - branch_codes: copied from signal_outcomes at submission time so the
--     Bayesian updater can attribute outcomes to branches without a join.
--   - Unique index on (api_key_hash, signal_id): one submission per user per
--     signal, enforced at the DB level.

CREATE TABLE IF NOT EXISTS community_outcomes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id       UUID NOT NULL REFERENCES signal_outcomes(signal_id) ON DELETE CASCADE,
    api_key_hash    VARCHAR(64) NOT NULL,
    did_buy         BOOLEAN NOT NULL,
    outcome         VARCHAR(20),  -- 'win' | 'lose' | 'break_even' | NULL (pending)
    submitted_at    TIMESTAMP DEFAULT NOW(),
    branch_codes    TEXT[],
    ticker          VARCHAR(10) NOT NULL,
    signal_date     DATE NOT NULL
);

-- Prevents duplicate submissions from the same user for the same signal.
CREATE UNIQUE INDEX IF NOT EXISTS idx_community_outcomes_dedup
    ON community_outcomes (api_key_hash, signal_id);

-- Supports efficient lookup of all community outcomes for a given signal.
CREATE INDEX IF NOT EXISTS idx_community_outcomes_signal
    ON community_outcomes (signal_id);

-- Partial index for the common Bayesian update query (settled outcomes only).
CREATE INDEX IF NOT EXISTS idx_community_outcomes_settled
    ON community_outcomes (signal_id) WHERE outcome IS NOT NULL;
