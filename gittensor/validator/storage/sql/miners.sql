-- Miners table
-- Stores miner identity and metadata information
-- Central table for miner identification across the system

CREATE TABLE IF NOT EXISTS miners (
    uid                 INTEGER          NOT NULL,
    hotkey              VARCHAR(255)     NOT NULL,
    github_id           VARCHAR(255)     NOT NULL,

    -- Metadata with automatic timestamps
    created_at          TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),
    updated_at          TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),

    -- Composite primary key
    PRIMARY KEY (uid, hotkey, github_id),

    -- Data integrity constraints
    CONSTRAINT chk_miners_uid                   CHECK (uid >= 0)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_miners_uid             ON miners (uid);
CREATE INDEX IF NOT EXISTS idx_miners_hotkey          ON miners (hotkey);
CREATE INDEX IF NOT EXISTS idx_miners_github_id       ON miners (github_id);
