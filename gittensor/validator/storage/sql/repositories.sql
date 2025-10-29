-- Repository information table
-- Stores basic repository metadata from classes.py Repository dataclass
-- Indexes for performance
CREATE TABLE IF NOT EXISTS repositories (
    full_name      VARCHAR(255)   PRIMARY KEY,
    name           VARCHAR(255)   NOT NULL,
    owner          VARCHAR(255)   NOT NULL,
    weight         DECIMAL(10,2)  NOT NULL,

    -- Activity status (NULL = active, timestamp = inactive)
    inactive_at    TIMESTAMPTZ    DEFAULT NULL,

    -- Metadata (stored in UTC, automatically converted to your session time zone when queried)
    created_at     TIMESTAMPTZ    DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for faster lookups and filtering
CREATE INDEX IF NOT EXISTS idx_repositories_name ON repositories (name);
CREATE INDEX IF NOT EXISTS idx_repositories_inactive_at ON repositories (inactive_at);
