-- File changes table
-- Stores individual file changes from classes.py FileChange dataclass

CREATE TABLE IF NOT EXISTS file_changes (
    id                   BIGSERIAL        PRIMARY KEY,
    pr_number            INTEGER          NOT NULL,
    repository_full_name VARCHAR(255)     NOT NULL,
    filename             VARCHAR(500)     NOT NULL,
    changes              INTEGER          DEFAULT 0,
    additions            INTEGER          DEFAULT 0,
    deletions            INTEGER          DEFAULT 0,
    status               VARCHAR(50)      NOT NULL,
    file_extension       VARCHAR(50)      NOT NULL DEFAULT '',
    patch                TEXT,

    -- Metadata with automatic timestamps
    created_at           TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),
    updated_at           TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),

    -- Foreign key constraint
    FOREIGN KEY (pr_number, repository_full_name)
        REFERENCES pull_requests(number, repository_full_name)
            ON DELETE CASCADE,

    -- Unique constraint to prevent duplicate file changes for same PR
    CONSTRAINT unique_file_change
        UNIQUE (pr_number, repository_full_name, filename),

    -- Data integrity constraints
    CONSTRAINT chk_file_changes_additions    CHECK    (additions >= 0),
    CONSTRAINT chk_file_changes_deletions    CHECK    (deletions >= 0),
    CONSTRAINT chk_file_changes_changes      CHECK    (changes   >= 0)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_file_changes_pr_number          ON file_changes (pr_number);
CREATE INDEX IF NOT EXISTS idx_file_changes_repository_name    ON file_changes (repository_full_name);
CREATE INDEX IF NOT EXISTS idx_file_changes_filename           ON file_changes (filename);
CREATE INDEX IF NOT EXISTS idx_file_changes_file_extension     ON file_changes (file_extension);
CREATE INDEX IF NOT EXISTS idx_file_changes_status             ON file_changes (status);