-- Pull request information table
-- Stores PR metadata from classes.py PullRequest dataclass

CREATE TABLE IF NOT EXISTS pull_requests (
    number               INTEGER          NOT NULL,
    repository_full_name VARCHAR(255)     NOT NULL,
    uid                  INTEGER          NOT NULL,
    hotkey               VARCHAR(255)     NOT NULL,
    github_id            VARCHAR(255)     NOT NULL,
    base_score           DECIMAL(15,6)    DEFAULT 0.0,
    earned_score         DECIMAL(15,6)    DEFAULT 0.0,
    title                TEXT             NOT NULL,
    merged_at            TIMESTAMP,        -- Nullable for draft PRs
    pr_created_at        TIMESTAMP        NOT NULL,
    additions            INTEGER          DEFAULT 0,
    deletions            INTEGER          DEFAULT 0,
    commits              INTEGER          DEFAULT 0,
    author_login         VARCHAR(255)     NOT NULL,
    merged_by_login      VARCHAR(255),

    -- Metadata with automatic timestamps
    created_at           TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),
    updated_at           TIMESTAMP        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'),

    PRIMARY KEY (number, repository_full_name),

    -- Foreign key constraints
    FOREIGN KEY (uid, hotkey, github_id)
        REFERENCES miners(uid, hotkey, github_id)
            ON DELETE CASCADE,

    -- Data integrity constraints
    CONSTRAINT chk_pull_requests_additions     CHECK    (additions >= 0),
    CONSTRAINT chk_pull_requests_deletions     CHECK    (deletions >= 0),
    CONSTRAINT chk_pull_requests_commits       CHECK    (commits   >= 0),
    CONSTRAINT chk_pull_requests_base_score    CHECK    (base_score >= 0),
    CONSTRAINT chk_pull_requests_earned_score  CHECK    (earned_score >= 0)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_pull_requests_author        ON pull_requests (author_login);
CREATE INDEX IF NOT EXISTS idx_pull_requests_merged_at     ON pull_requests (merged_at);
CREATE INDEX IF NOT EXISTS idx_pull_requests_merged_by     ON pull_requests (merged_by_login);
CREATE INDEX IF NOT EXISTS idx_pull_requests_uid           ON pull_requests (uid);
CREATE INDEX IF NOT EXISTS idx_pull_requests_hotkey        ON pull_requests (hotkey);
CREATE INDEX IF NOT EXISTS idx_pull_requests_github_id     ON pull_requests (github_id);
CREATE INDEX IF NOT EXISTS idx_pull_requests_earned_score  ON pull_requests (earned_score);
