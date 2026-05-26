-- Migration 011: Unified OHLCV daily price table
-- All OHLCV data (FinMind + yfinance) stored here with source tracking.
-- FinMindClient uses DB-first pattern: read from DB, fill gaps via API.

CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker      VARCHAR(10)   NOT NULL,
    trade_date  DATE          NOT NULL,
    open        NUMERIC(12,2),
    high        NUMERIC(12,2),
    low         NUMERIC(12,2),
    close       NUMERIC(12,2),
    volume      BIGINT,
    source      VARCHAR(20)   NOT NULL DEFAULT 'finmind',
    fetched_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, trade_date)
);

CREATE INDEX IF NOT EXISTS ohlcv_daily_date_idx ON ohlcv_daily(trade_date DESC);
