# Storage Queries - Only SET/INSERT operations for writing data

# Miner Queries
SET_MINER = """
INSERT INTO miners (uid, hotkey, github_id)
VALUES (%s, %s, %s)
ON CONFLICT (uid, hotkey, github_id)
DO NOTHING
"""

# Pull Request Queries
BULK_UPSERT_PULL_REQUESTS = """
INSERT INTO pull_requests (
    number, repository_full_name, uid, hotkey, github_id, earned_score,
    title, merged_at, pr_created_at, additions, deletions, commits,
    author_login, merged_by_login
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO UPDATE SET
    uid = EXCLUDED.uid,
    hotkey = EXCLUDED.hotkey,
    earned_score = EXCLUDED.earned_score,
    updated_at = NOW()
"""

# Issue Queries
BULK_UPSERT_ISSUES = """
INSERT INTO issues (
    number, pr_number, repository_full_name, title, created_at, closed_at
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO NOTHING
"""

# File Change Queries
BULK_UPSERT_FILE_CHANGES = """
INSERT INTO file_changes (
    pr_number, repository_full_name, filename, changes, additions, deletions, status, patch, file_extension
) VALUES %s
ON CONFLICT (pr_number, repository_full_name, filename)
DO NOTHING
"""

# Miner Evaluation Queries
SET_MINER_EVALUATION = """
INSERT INTO miner_evaluations (
    uid, hotkey, github_id, failed_reason, total_score,
    total_lines_changed, total_open_prs, total_prs, unique_repos_count
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
