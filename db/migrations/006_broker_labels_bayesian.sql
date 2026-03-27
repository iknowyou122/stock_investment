-- Migration 006: Add community Bayesian win-rate columns to broker_labels.
--
-- These columns accumulate community-reported trade outcomes per broker branch
-- and drive the Laplace-smoothed Beta-Bernoulli posterior win-rate estimate.
--
-- Column semantics:
--   community_win_count     — count of 'win' outcomes submitted by community users
--                             for signals where this branch appeared in branch_codes.
--   community_sample_count  — count of all settled (non-NULL) community outcomes
--                             for signals where this branch appeared.
--   community_signal_win_rate — Laplace-smoothed posterior:
--                               (community_win_count + 1) / (community_sample_count + 2)
--                             NULL means no community data has been accumulated yet.
--                             Shown as NULL to API clients (not 0.5) to avoid
--                             implying false confidence.
--
-- NOTE: This is intentionally SEPARATE from reversal_rate, which is derived
-- from FinMind 分點 D+2 historical reversal data. community_signal_win_rate
-- reflects community-reported forward outcomes on actual trades.
--
-- Safe to re-run: IF NOT EXISTS guards prevent double-application.

ALTER TABLE broker_labels
    ADD COLUMN IF NOT EXISTS community_win_count    INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS community_sample_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS community_signal_win_rate FLOAT;

COMMENT ON COLUMN broker_labels.community_win_count IS
    'Count of community-reported win outcomes for signals containing this branch.';

COMMENT ON COLUMN broker_labels.community_sample_count IS
    'Count of all settled community outcomes for signals containing this branch.';

COMMENT ON COLUMN broker_labels.community_signal_win_rate IS
    'Laplace-smoothed Beta-Bernoulli posterior: (wins+1)/(samples+2). NULL = no data yet.';
