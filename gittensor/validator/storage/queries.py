# Storage Queries - Only SET/INSERT operations for writing data

# Miner Queries
SET_MINER = """
INSERT INTO miners (uid, hotkey, github_id)
VALUES (%s, %s, %s)
ON CONFLICT (uid, hotkey, github_id)
DO NOTHING
"""

UPSERT_MINER = """
INSERT INTO miners (uid, hotkey, github_id)
VALUES (%s, %s, %s)
ON CONFLICT (uid, hotkey, github_id)
DO UPDATE SET
    updated_at = CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'
"""

# Repository Queries
SET_REPOSITORY = """
INSERT INTO repositories (full_name, name, owner, weight)
VALUES (%s, %s, %s, %s)
ON CONFLICT (full_name)
DO NOTHING
"""

# Pull Request Queries
# NOTE: changed this to update so that PRs will reflect the current uid/hotkey/score
SET_PULL_REQUEST = """
  INSERT INTO pull_requests (
      number, repository_full_name, uid, hotkey, github_id, earned_score,
      title, merged_at, pr_created_at, additions, deletions, commits,
      author_login, merged_by_login
  ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
  ON CONFLICT (number, repository_full_name)
  DO UPDATE SET
      uid = EXCLUDED.uid,
      hotkey = EXCLUDED.hotkey,
      github_id = EXCLUDED.github_id,
      earned_score = EXCLUDED.earned_score,
      title = EXCLUDED.title,
      merged_at = EXCLUDED.merged_at,
      additions = EXCLUDED.additions,
      deletions = EXCLUDED.deletions,
      commits = EXCLUDED.commits,
      author_login = EXCLUDED.author_login,
      merged_by_login = EXCLUDED.merged_by_login,
      updated_at = CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'
  """
# File Change Queries
SET_FILE_CHANGES_FOR_PR = """
INSERT INTO file_changes (
    pr_number, repository_full_name, filename, changes, additions, deletions, status, patch, file_extension
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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

# Issue Queries
SET_ISSUE = """
INSERT INTO issues (
    number, pr_number, repository_full_name, title, created_at, closed_at
) VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (number, repository_full_name)
DO NOTHING
"""

# Bulk Upsert Queries
BULK_UPSERT_MINERS = """
INSERT INTO miners (uid, hotkey, github_id)
VALUES %s
ON CONFLICT (uid, hotkey, github_id)
DO UPDATE SET
    updated_at = CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago'
"""

BULK_UPSERT_REPOSITORIES = """
INSERT INTO repositories (full_name, name, owner, weight)
VALUES %s
ON CONFLICT (full_name)
DO NOTHING
"""

BULK_UPSERT_PULL_REQUESTS = """
INSERT INTO pull_requests (
    number, repository_full_name, uid, hotkey, github_id, earned_score,
    title, merged_at, pr_created_at, additions, deletions, commits,
    author_login, merged_by_login
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO NOTHING
"""

BULK_UPSERT_ISSUES = """
INSERT INTO issues (
    number, pr_number, repository_full_name, title, created_at, closed_at
) VALUES %s
ON CONFLICT (number, repository_full_name)
DO NOTHING
"""

BULK_UPSERT_FILE_CHANGES = """
INSERT INTO file_changes (
    pr_number, repository_full_name, filename, changes, additions, deletions, status, patch, file_extension
) VALUES %s
ON CONFLICT (pr_number, repository_full_name, filename)
DO NOTHING
"""
