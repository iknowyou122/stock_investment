-- Migration 010: Surge signals & watch tracker → DB
-- Removes CSV dependency; surge data now stored in surge_signals + surge_watch tables.

-- 1. Extend signal_outcomes.source to include 'surge'
ALTER TABLE signal_outcomes
    DROP CONSTRAINT IF EXISTS source_valid;

ALTER TABLE signal_outcomes
    ADD CONSTRAINT source_valid
    CHECK (source IN ('live', 'backtest', 'replay', 'sandbox', 'surge'));

-- 2. Surge scan results table
CREATE TABLE IF NOT EXISTS surge_signals (
    id              SERIAL PRIMARY KEY,
    analysis_date   DATE NOT NULL,
    scan_date       DATE NOT NULL,
    ticker          VARCHAR(10) NOT NULL,
    name            VARCHAR(100),
    market          VARCHAR(10),
    industry        VARCHAR(100),
    grade           VARCHAR(20),
    score           FLOAT,
    vol_ratio       FLOAT,
    close_strength  FLOAT,
    day_chg_pct     FLOAT,
    gap_pct         FLOAT,
    surge_day       INT,
    industry_rank_pct FLOAT,
    rsi             FLOAT,
    inst_consec_days INT,
    close_price     FLOAT,
    score_breakdown JSONB,
    flags           TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (analysis_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_surge_signals_date ON surge_signals (analysis_date);
CREATE INDEX IF NOT EXISTS idx_surge_signals_grade ON surge_signals (analysis_date, grade);

-- 3. D+1 tracking table (replaces watch_YYYY-MM-DD.json)
CREATE TABLE IF NOT EXISTS surge_watch (
    id              SERIAL PRIMARY KEY,
    scan_date       DATE NOT NULL,
    ticker          VARCHAR(10) NOT NULL,
    name            VARCHAR(100),
    market          VARCHAR(10),
    industry        VARCHAR(100),
    score           FLOAT,
    close_price     FLOAT,
    vol_ratio       FLOAT,
    close_strength  FLOAT,
    day_chg_pct     FLOAT,
    flags           TEXT,
    d1_confirmed    BOOLEAN DEFAULT FALSE,
    close_d1        FLOAT,
    d1_chg_pct      FLOAT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (scan_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_surge_watch_date ON surge_watch (scan_date);
CREATE INDEX IF NOT EXISTS idx_surge_watch_unconfirmed ON surge_watch (scan_date) WHERE d1_confirmed = FALSE;
