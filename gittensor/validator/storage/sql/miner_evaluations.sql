-- Miner evaluation results table
-- Stores complete validator assessment results from classes.py MinerEvaluation dataclass

CREATE TABLE IF NOT EXISTS miner_evaluations (
    id                   BIGSERIAL        PRIMARY KEY,
    uid                  INTEGER          NOT NULL,
    hotkey               VARCHAR(255)     NOT NULL,
    github_id            VARCHAR(255)     NOT NULL,
    failed_reason        TEXT,
    total_score          DECIMAL(15,6)    DEFAULT 0.0,
    total_lines_changed  INTEGER          DEFAULT 0,
    total_open_prs       INTEGER          DEFAULT 0,
    total_prs            INTEGER          DEFAULT 0,
    unique_repos_count   INTEGER          DEFAULT 0,

    -- Metadata with automatic timestamps
    evaluation_timestamp TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),
    created_at           TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),
    updated_at           TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),

    -- Foreign key constraint to miners table
    FOREIGN KEY (uid, hotkey, github_id)
        REFERENCES miners(uid, hotkey, github_id)
            ON DELETE CASCADE,

    -- Unique constraint to prevent duplicate evaluations
    CONSTRAINT unique_evaluation
        UNIQUE (uid, hotkey, evaluation_timestamp)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_miner_evaluations_uid                     ON miner_evaluations (uid);
CREATE INDEX IF NOT EXISTS idx_miner_evaluations_hotkey                  ON miner_evaluations (hotkey);
CREATE INDEX IF NOT EXISTS idx_miner_evaluations_github_id               ON miner_evaluations (github_id);
CREATE INDEX IF NOT EXISTS idx_miner_evaluations_evaluation_timestamp    ON miner_evaluations (evaluation_timestamp);