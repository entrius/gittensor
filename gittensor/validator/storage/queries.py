# Storage Queries - Only SET/INSERT operations for writing data

# Cleanup Queries - Remove stale data when a miner re-registers on a new uid/hotkey
CLEANUP_STALE_MINER_EVALUATIONS = """
DELETE FROM miner_evaluations
WHERE github_id = %s
  AND github_id != '0'
  AND (uid != %s OR hotkey != %s)
  AND created_at <= %s
"""

CLEANUP_STALE_MINER_TIER_STATS = """
DELETE FROM miner_tier_stats
WHERE github_id = %s
  AND github_id != '0'
  AND (uid != %s OR hotkey != %s)
"""

CLEANUP_STALE_MINERS = """
DELETE FROM miners
WHERE github_id = %s
  AND github_id != '0'
  AND (uid != %s OR hotkey != %s)
"""

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
    credibility_multiplier, raw_credibility, credibility_scalar,
    earned_score, collateral_score,
    additions, deletions, commits, total_nodes_scored,
    merged_by_login, description, last_edited_at,
    token_score, structural_count, structural_score, leaf_count, leaf_score
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO UPDATE SET
    uid = EXCLUDED.uid,
    hotkey = EXCLUDED.hotkey,
    title = EXCLUDED.title,
    author_login = EXCLUDED.author_login,
    merged_at = EXCLUDED.merged_at,
    pr_state = EXCLUDED.pr_state,
    repo_weight_multiplier = EXCLUDED.repo_weight_multiplier,
    base_score = EXCLUDED.base_score,
    issue_multiplier = EXCLUDED.issue_multiplier,
    open_pr_spam_multiplier = EXCLUDED.open_pr_spam_multiplier,
    repository_uniqueness_multiplier = EXCLUDED.repository_uniqueness_multiplier,
    time_decay_multiplier = EXCLUDED.time_decay_multiplier,
    credibility_multiplier = EXCLUDED.credibility_multiplier,
    raw_credibility = EXCLUDED.raw_credibility,
    credibility_scalar = EXCLUDED.credibility_scalar,
    earned_score = EXCLUDED.earned_score,
    collateral_score = EXCLUDED.collateral_score,
    additions = EXCLUDED.additions,
    deletions = EXCLUDED.deletions,
    commits = EXCLUDED.commits,
    total_nodes_scored = EXCLUDED.total_nodes_scored,
    merged_by_login = EXCLUDED.merged_by_login,
    description = EXCLUDED.description,
    last_edited_at = EXCLUDED.last_edited_at,
    token_score = EXCLUDED.token_score,
    structural_count = EXCLUDED.structural_count,
    structural_score = EXCLUDED.structural_score,
    leaf_count = EXCLUDED.leaf_count,
    leaf_score = EXCLUDED.leaf_score,
    updated_at = NOW()
"""

# Issue Queries
BULK_UPSERT_ISSUES = """
INSERT INTO issues (
    number, pr_number, repository_full_name, title, created_at, closed_at,
    author_login, state, author_association
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO UPDATE SET
    title = EXCLUDED.title,
    closed_at = EXCLUDED.closed_at,
    author_login = EXCLUDED.author_login,
    state = EXCLUDED.state,
    author_association = EXCLUDED.author_association
"""

# File Change Queries
BULK_UPSERT_FILE_CHANGES = """
INSERT INTO file_changes (
    pr_number, repository_full_name, filename, changes, additions, deletions, status, patch, file_extension
) VALUES %s
ON CONFLICT (pr_number, repository_full_name, filename)
DO UPDATE SET
    changes = EXCLUDED.changes,
    additions = EXCLUDED.additions,
    deletions = EXCLUDED.deletions,
    status = EXCLUDED.status,
    patch = EXCLUDED.patch,
    file_extension = EXCLUDED.file_extension
"""

# Miner Evaluation Queries
BULK_UPSERT_MINER_EVALUATION = """
INSERT INTO miner_evaluations (
    uid, hotkey, github_id, failed_reason, base_total_score, total_score, total_collateral_score,
    total_nodes_scored, total_open_prs, total_closed_prs, total_merged_prs, total_prs,
    unique_repos_count, qualified_unique_repos_count,
    current_tier,
    total_token_score, total_structural_count, total_structural_score, total_leaf_count, total_leaf_score
) VALUES %s
ON CONFLICT (uid, hotkey, github_id)
DO UPDATE SET
    failed_reason = EXCLUDED.failed_reason,
    base_total_score = EXCLUDED.base_total_score,
    total_score = EXCLUDED.total_score,
    total_collateral_score = EXCLUDED.total_collateral_score,
    total_nodes_scored = EXCLUDED.total_nodes_scored,
    total_open_prs = EXCLUDED.total_open_prs,
    total_closed_prs = EXCLUDED.total_closed_prs,
    total_merged_prs = EXCLUDED.total_merged_prs,
    total_prs = EXCLUDED.total_prs,
    unique_repos_count = EXCLUDED.unique_repos_count,
    qualified_unique_repos_count = EXCLUDED.qualified_unique_repos_count,
    current_tier = EXCLUDED.current_tier,
    total_token_score = EXCLUDED.total_token_score,
    total_structural_count = EXCLUDED.total_structural_count,
    total_structural_score = EXCLUDED.total_structural_score,
    total_leaf_count = EXCLUDED.total_leaf_count,
    total_leaf_score = EXCLUDED.total_leaf_score,
    updated_at = NOW()
"""

# Miner Tier Stats Queries (joins on uid, hotkey, github_id)
BULK_UPSERT_MINER_TIER_STATS = """
INSERT INTO miner_tier_stats (
    uid, hotkey, github_id,
    bronze_merged_prs, bronze_closed_prs, bronze_total_prs, bronze_collateral_score, bronze_score,
    bronze_unique_repos, bronze_qualified_unique_repos,
    bronze_token_score, bronze_structural_count, bronze_structural_score, bronze_leaf_count, bronze_leaf_score,
    silver_merged_prs, silver_closed_prs, silver_total_prs, silver_collateral_score, silver_score,
    silver_unique_repos, silver_qualified_unique_repos,
    silver_token_score, silver_structural_count, silver_structural_score, silver_leaf_count, silver_leaf_score,
    gold_merged_prs, gold_closed_prs, gold_total_prs, gold_collateral_score, gold_score,
    gold_unique_repos, gold_qualified_unique_repos,
    gold_token_score, gold_structural_count, gold_structural_score, gold_leaf_count, gold_leaf_score
) VALUES %s
ON CONFLICT (uid, hotkey, github_id)
DO UPDATE SET
    bronze_merged_prs = EXCLUDED.bronze_merged_prs,
    bronze_closed_prs = EXCLUDED.bronze_closed_prs,
    bronze_total_prs = EXCLUDED.bronze_total_prs,
    bronze_collateral_score = EXCLUDED.bronze_collateral_score,
    bronze_score = EXCLUDED.bronze_score,
    bronze_unique_repos = EXCLUDED.bronze_unique_repos,
    bronze_qualified_unique_repos = EXCLUDED.bronze_qualified_unique_repos,
    bronze_token_score = EXCLUDED.bronze_token_score,
    bronze_structural_count = EXCLUDED.bronze_structural_count,
    bronze_structural_score = EXCLUDED.bronze_structural_score,
    bronze_leaf_count = EXCLUDED.bronze_leaf_count,
    bronze_leaf_score = EXCLUDED.bronze_leaf_score,
    silver_merged_prs = EXCLUDED.silver_merged_prs,
    silver_closed_prs = EXCLUDED.silver_closed_prs,
    silver_total_prs = EXCLUDED.silver_total_prs,
    silver_collateral_score = EXCLUDED.silver_collateral_score,
    silver_score = EXCLUDED.silver_score,
    silver_unique_repos = EXCLUDED.silver_unique_repos,
    silver_qualified_unique_repos = EXCLUDED.silver_qualified_unique_repos,
    silver_token_score = EXCLUDED.silver_token_score,
    silver_structural_count = EXCLUDED.silver_structural_count,
    silver_structural_score = EXCLUDED.silver_structural_score,
    silver_leaf_count = EXCLUDED.silver_leaf_count,
    silver_leaf_score = EXCLUDED.silver_leaf_score,
    gold_merged_prs = EXCLUDED.gold_merged_prs,
    gold_closed_prs = EXCLUDED.gold_closed_prs,
    gold_total_prs = EXCLUDED.gold_total_prs,
    gold_collateral_score = EXCLUDED.gold_collateral_score,
    gold_score = EXCLUDED.gold_score,
    gold_unique_repos = EXCLUDED.gold_unique_repos,
    gold_qualified_unique_repos = EXCLUDED.gold_qualified_unique_repos,
    gold_token_score = EXCLUDED.gold_token_score,
    gold_structural_count = EXCLUDED.gold_structural_count,
    gold_structural_score = EXCLUDED.gold_structural_score,
    gold_leaf_count = EXCLUDED.gold_leaf_count,
    gold_leaf_score = EXCLUDED.gold_leaf_score,
    updated_at = NOW()
"""
