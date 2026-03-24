-- Migration 002: signal_outcomes table
-- Tracks historical signal outputs and their actual market outcomes.
-- Used for backtesting, walk-forward validation, and Phase 2 lift measurement.
--
-- Populated by:
--   1. StrategistAgent.run() (inserts signal at time of generation)
--   2. A nightly outcome-recorder job (updates actual_* fields D+2 after signal date)

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id              SERIAL        PRIMARY KEY,
    ticker          VARCHAR(10)   NOT NULL,
    signal_date     DATE          NOT NULL,   -- T+0: date of analysis (T+1 settlement)
    action          VARCHAR(10)   NOT NULL,   -- 'LONG' | 'WATCH' | 'CAUTION'
    confidence      INT           NOT NULL,   -- 0–100
    entry_bid_limit FLOAT         NOT NULL,   -- close * 0.995
    entry_max_chase FLOAT         NOT NULL,   -- close * 1.005
    stop_loss       FLOAT         NOT NULL,   -- T+0 closing price
    target          FLOAT         NOT NULL,   -- poc_proxy * 1.05

    -- Chip metrics at signal time (for regression analysis)
    concentration_top15     FLOAT,
    net_buyer_count_diff    INT,
    active_branch_count     INT,
    risk_flags              TEXT[],           -- e.g. {'隔日沖_TOP3'}
    data_quality_flags      TEXT[],

    -- Actual outcomes (filled in D+2 by outcome recorder)
    execution_date          DATE,             -- D+2 (first tradeable day)
    actual_open_d2          FLOAT,            -- D+2 open price
    actual_close_d2         FLOAT,            -- D+2 close price
    actual_close_d5         FLOAT,            -- D+5 close (5-day holding)
    outcome_pnl_pct         FLOAT,            -- (actual_close_d2 - entry_max_chase) / entry_max_chase
    outcome_hit_target      BOOLEAN,          -- close_d5 >= target
    outcome_hit_stop        BOOLEAN,          -- close_d2 <= stop_loss

    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT action_valid CHECK (action IN ('LONG', 'WATCH', 'CAUTION')),
    CONSTRAINT confidence_range CHECK (confidence >= 0 AND confidence <= 100),
    UNIQUE (ticker, signal_date)             -- one signal per ticker per day
);

-- Indexes for backtesting queries
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_ticker_date
    ON signal_outcomes (ticker, signal_date DESC);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_action
    ON signal_outcomes (action, signal_date DESC);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_confidence
    ON signal_outcomes (confidence DESC, signal_date DESC);

-- Auto-update updated_at on row change
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER signal_outcomes_updated_at
    BEFORE UPDATE ON signal_outcomes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE signal_outcomes IS
    'Historical record of all Triple Confirmation signals and their D+2/D+5 outcomes. '
    'Used for walk-forward backtesting and lift measurement against unconditional baseline.';

COMMENT ON COLUMN signal_outcomes.outcome_pnl_pct IS
    'Realized P&L assuming entry at entry_max_chase and exit at D+2 close. '
    'Does not include transaction costs (assume 0.3% round-trip for evaluation).';
