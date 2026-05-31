-- Migration 012: Fixed-horizon return columns for signal_outcomes
-- Stores T+1/T+3/T+5 calendar-bar returns so accuracy_monitor can
-- write back verified P&L and batch_plan can display "昨日戰績".

ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS return_t1  FLOAT,
    ADD COLUMN IF NOT EXISTS return_t3  FLOAT,
    ADD COLUMN IF NOT EXISTS return_t5  FLOAT;

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_returns
    ON signal_outcomes (signal_date, source, action)
    WHERE return_t5 IS NOT NULL;
