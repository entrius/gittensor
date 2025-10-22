-- Issues table
-- Stores issue information referenced by pull requests

CREATE TABLE IF NOT EXISTS issues (
    number               INTEGER          NOT NULL,
    pr_number            INTEGER          NOT NULL,
    repository_full_name VARCHAR(255)     NOT NULL,
    title                TEXT             NOT NULL,
    created_at           TIMESTAMP,        -- Nullable for API compatibility
    closed_at            TIMESTAMP,        -- Nullable for API compatibility

    PRIMARY KEY (number, repository_full_name),

    -- Foreign key constraint
    FOREIGN KEY (pr_number, repository_full_name)
        REFERENCES pull_requests(number, repository_full_name)
            ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_issues_pr_number     ON issues (pr_number);
CREATE INDEX IF NOT EXISTS idx_issues_repository    ON issues (repository_full_name);
CREATE INDEX IF NOT EXISTS idx_issues_created_at    ON issues (created_at);
CREATE INDEX IF NOT EXISTS idx_issues_closed_at     ON issues (closed_at);