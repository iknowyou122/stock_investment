CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker VARCHAR(10) NOT NULL,
    signal_date DATE NOT NULL,
    confidence_score INT NOT NULL,
    action VARCHAR(10) NOT NULL,
    entry_price FLOAT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    price_1d FLOAT,
    price_3d FLOAT,
    price_5d FLOAT,
    outcome_1d FLOAT,
    outcome_3d FLOAT,
    outcome_5d FLOAT,
    halt_flag BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_ticker_date ON signal_outcomes (ticker, signal_date);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_unsettled ON signal_outcomes (created_at) WHERE price_5d IS NULL AND halt_flag = FALSE;
