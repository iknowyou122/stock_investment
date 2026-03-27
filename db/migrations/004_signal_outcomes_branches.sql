-- Migration 004: Add branch_codes column to signal_outcomes.
--
-- branch_codes stores the broker branch codes associated with the signal
-- at generation time, enabling community outcome data to be linked back
-- to specific broker branches for Bayesian win-rate updates.
--
-- Safe to re-run: IF NOT EXISTS guard prevents double-application.

ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS branch_codes TEXT[] DEFAULT ARRAY[]::TEXT[];
