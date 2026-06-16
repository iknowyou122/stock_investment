-- Phase 4.49: Simulated holdings table for "what to buy / hold / sell" continuity.
--
-- The AllocationAdvisor records each recommended position here so subsequent
-- daily runs can continue tracking it (not re-recommend from scratch every
-- day). Closes triggered by stop loss / take profit / time stop / Tier drop
-- write the close_reason and close_date.

CREATE TABLE IF NOT EXISTS simulated_holdings (
    holding_id      SERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    entry_date      DATE NOT NULL,
    entry_price     NUMERIC(10, 2) NOT NULL,
    suggested_pct   NUMERIC(5, 2) NOT NULL,    -- 0-30 (% of portfolio at entry)
    tier            CHAR(1) NOT NULL,           -- S/A/B/C
    stop_loss       NUMERIC(10, 2) NOT NULL,
    take_profit     NUMERIC(10, 2) NOT NULL,
    industry        TEXT,
    concept_keys    TEXT,                       -- comma-separated keys
    entry_reason    TEXT,                       -- LLM reasoning at entry
    status          TEXT NOT NULL DEFAULT 'OPEN', -- OPEN | CLOSED | REDUCED
    close_date      DATE,
    close_price     NUMERIC(10, 2),
    close_reason    TEXT,                       -- STOP_LOSS / TAKE_PROFIT / TIME_STOP / TIER_DROP / MANUAL
    realised_pct    NUMERIC(7, 2),              -- final % return when closed
    notes           TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, entry_date)
);

CREATE INDEX IF NOT EXISTS idx_holdings_status ON simulated_holdings (status);
CREATE INDEX IF NOT EXISTS idx_holdings_ticker ON simulated_holdings (ticker);
CREATE INDEX IF NOT EXISTS idx_holdings_entry_date ON simulated_holdings (entry_date DESC);
