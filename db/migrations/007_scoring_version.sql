-- Migration 007: add scoring_version to signal_outcomes
-- Must run before first v2 signal is written
ALTER TABLE signal_outcomes ADD COLUMN scoring_version TEXT NOT NULL DEFAULT 'v1';
