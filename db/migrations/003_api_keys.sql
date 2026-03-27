CREATE TABLE IF NOT EXISTS api_keys (
    key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    api_key VARCHAR(64) UNIQUE NOT NULL,
    tier VARCHAR(20) NOT NULL DEFAULT 'free',   -- 'free' | 'pro'
    email VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    monthly_request_count INT DEFAULT 0,
    count_reset_at TIMESTAMP DEFAULT DATE_TRUNC('month', NOW()),
    is_active BOOLEAN DEFAULT TRUE
);
