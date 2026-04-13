-- Migration 009: Daily post-market review support
-- Adds T+1 intraday columns, entry_success tracking, and A/B param competition table.

-- signal_outcomes: T+1 結算 + A/B 欄位
ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS stop_loss          FLOAT,
    ADD COLUMN IF NOT EXISTS intraday_high      FLOAT,
    ADD COLUMN IF NOT EXISTS intraday_low       FLOAT,
    ADD COLUMN IF NOT EXISTS entry_success      BOOLEAN,
    ADD COLUMN IF NOT EXISTS ab_candidate_score INT;

-- 快速查詢待 T+1 結算的 LONG 信號
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_review
    ON signal_outcomes (signal_date, source, action)
    WHERE entry_success IS NULL AND halt_flag = FALSE;

-- A/B 參數競賽表
CREATE TABLE IF NOT EXISTS ab_competitions (
    id                    SERIAL PRIMARY KEY,
    started_at            DATE NOT NULL DEFAULT CURRENT_DATE,
    params_active         JSONB NOT NULL,
    params_candidate      JSONB NOT NULL,
    reason                TEXT,
    lift_estimate         FLOAT,
    signals_active_wins   INT DEFAULT 0,
    signals_active_total  INT DEFAULT 0,
    signals_cand_wins     INT DEFAULT 0,
    signals_cand_total    INT DEFAULT 0,
    status                VARCHAR(20) NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'promoted', 'discarded')),
    resolved_at           DATE,
    resolution_note       TEXT
);
