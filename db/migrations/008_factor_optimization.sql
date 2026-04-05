-- Migration 008: Factor optimization tracking
-- Adds score_breakdown + source to signal_outcomes, creates factor lifecycle tables.

ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS score_breakdown JSONB,
    ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'live';

-- Factor lifecycle registry
CREATE TABLE IF NOT EXISTS factor_registry (
    name               VARCHAR(50) PRIMARY KEY,
    status             VARCHAR(20) NOT NULL DEFAULT 'experimental',
    -- 'experimental' | 'active' | 'deprecated'
    lift_30d           FLOAT,
    lift_90d           FLOAT,
    added_date         DATE NOT NULL DEFAULT CURRENT_DATE,
    deprecated_date    DATE,
    notes              TEXT
);

-- Seed known active factors
INSERT INTO factor_registry (name, status, added_date)
VALUES
    ('RSI_MOM',           'active', CURRENT_DATE),
    ('BREAKOUT_WITH_VOL', 'active', CURRENT_DATE),
    ('GATE_VOL_MET',      'active', CURRENT_DATE),
    ('GATE_TREND_MET',    'active', CURRENT_DATE)
ON CONFLICT (name) DO NOTHING;

-- Engine parameter change history
CREATE TABLE IF NOT EXISTS engine_versions (
    id             SERIAL PRIMARY KEY,
    applied_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    params_before  JSONB NOT NULL,
    params_after   JSONB NOT NULL,
    reason         TEXT,
    lift_estimate  FLOAT
);
