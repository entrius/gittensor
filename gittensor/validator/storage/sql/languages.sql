-- Repository information table
-- Stores basic repository metadata from classes.py Repository dataclass

CREATE TABLE IF NOT EXISTS languages (
    extension        VARCHAR(16)     PRIMARY KEY,
    weight           DECIMAL(10,2)    NOT NULL,

    -- Metadata with automatic timestamps
    created_at       TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),
    updated_at       TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago')
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_languages_extension ON languages (extension);
