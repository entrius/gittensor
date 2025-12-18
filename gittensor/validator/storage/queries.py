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
    number, repository_full_name, uid, hotkey, github_id, title, author_login,
    merged_at, pr_created_at, pr_state,
    repo_weight_multiplier, base_score, issue_multiplier,
    open_pr_spam_multiplier, repository_uniqueness_multiplier, time_decay_multiplier,
    gittensor_tag_multiplier, merge_success_multiplier, earned_score, collateral_score,
    additions, deletions, commits, total_lines_scored, gittensor_tagged,
    merged_by_login, description, last_edited_at
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO UPDATE SET
    uid = EXCLUDED.uid,
    hotkey = EXCLUDED.hotkey,
    pr_state = EXCLUDED.pr_state,
    repo_weight_multiplier = EXCLUDED.repo_weight_multiplier,
    base_score = EXCLUDED.base_score,
    issue_multiplier = EXCLUDED.issue_multiplier,
    open_pr_spam_multiplier = EXCLUDED.open_pr_spam_multiplier,
    repository_uniqueness_multiplier = EXCLUDED.repository_uniqueness_multiplier,
    time_decay_multiplier = EXCLUDED.time_decay_multiplier,
    gittensor_tag_multiplier = EXCLUDED.gittensor_tag_multiplier,
    merge_success_multiplier = EXCLUDED.merge_success_multiplier,
    earned_score = EXCLUDED.earned_score,
    collateral_score = EXCLUDED.collateral_score,
    total_lines_scored = EXCLUDED.total_lines_scored,
    gittensor_tagged = EXCLUDED.gittensor_tagged,
    description = EXCLUDED.description,
    last_edited_at = EXCLUDED.last_edited_at,
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
BULK_UPSERT_MINER_EVALUATION = """
INSERT INTO miner_evaluations (
    uid, hotkey, github_id, failed_reason, base_total_score, total_score, total_collateral_score,
    total_lines_changed, total_open_prs, total_closed_prs, total_merged_prs, total_prs, unique_repos_count
) VALUES %s
ON CONFLICT (uid, hotkey, github_id)
DO UPDATE SET
    failed_reason = EXCLUDED.failed_reason,
    base_total_score = EXCLUDED.base_total_score,
    total_score = EXCLUDED.total_score,
    total_collateral_score = EXCLUDED.total_collateral_score,
    total_lines_changed = EXCLUDED.total_lines_changed,
    total_open_prs = EXCLUDED.total_open_prs,
    total_closed_prs = EXCLUDED.total_closed_prs,
    total_merged_prs = EXCLUDED.total_merged_prs,
    total_prs = EXCLUDED.total_prs,
    unique_repos_count = EXCLUDED.unique_repos_count,
    updated_at = NOW()
"""
