-- Migration 001: broker_labels table
-- Stores behavioral labels for 分點 (broker branches) derived from historical trade data.
--
-- Classification rules (applied by BrokerLabelClassifier):
--   label = '隔日沖' if reversal_rate > 0.60 AND sample_count >= 50
--   label = 'unknown' until sample_count reaches 50
--
-- reversal_rate: P(D+2 close < D+0 close | branch in top-3 buyers on D+0)
--   D+2 horizon used because FinMind 分點 data is published T+1 night;
--   earliest tradeable execution is D+2 open.

CREATE TABLE IF NOT EXISTS broker_labels (
    branch_code     VARCHAR(20)   PRIMARY KEY,  -- e.g. '1480' (凱基台北)
    branch_name     VARCHAR(100)  NOT NULL,
    label           VARCHAR(20)   NOT NULL DEFAULT 'unknown',
                                  -- '隔日沖' | '波段贏家' | '地緣券商' | '代操官股' | 'unknown'
    reversal_rate   FLOAT         NOT NULL DEFAULT 0.0,
                                  -- P(D+2 close < D+0 close | branch in top-3)
    sample_count    INT           NOT NULL DEFAULT 0,
                                  -- number of top-3-buyer occurrences used to compute rate
    last_updated    DATE          NOT NULL,
    metadata        JSONB         NOT NULL DEFAULT '{}'::jsonb,
                                  -- arbitrary features for Phase 2+ classifiers

    CONSTRAINT label_valid CHECK (
        label IN ('隔日沖', '波段贏家', '地緣券商', '代操官股', 'unknown')
    ),
    CONSTRAINT reversal_rate_range CHECK (
        reversal_rate >= 0.0 AND reversal_rate <= 1.0
    ),
    CONSTRAINT sample_count_nonneg CHECK (sample_count >= 0)
);

-- Index for fast label lookups (e.g., "find all 隔日沖 branches")
CREATE INDEX IF NOT EXISTS idx_broker_labels_label
    ON broker_labels (label);

-- Index for finding stale records that need re-classification
CREATE INDEX IF NOT EXISTS idx_broker_labels_last_updated
    ON broker_labels (last_updated);

-- Comment for documentation
COMMENT ON TABLE broker_labels IS
    'Behavioral labels for Taiwan stock broker branches (分點). '
    'Updated by BrokerLabelClassifier.fit() when new historical data is available.';

COMMENT ON COLUMN broker_labels.reversal_rate IS
    'P(stock closes below D+0 close on D+2 | this branch is top-3 buyer on D+0). '
    'D+2 is earliest tradeable execution after FinMind T+1 data publish.';

COMMENT ON COLUMN broker_labels.sample_count IS
    'Number of (ticker, date) instances where this branch was top-3 buyer. '
    'Minimum 50 required for classification (set label=unknown below threshold).';
