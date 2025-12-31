#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Scoring Simulation Runner

Runs the full incentive mechanism scoring pipeline using data from the test database.
No miner queries, blockchain, or GitHub API calls (file_changes loaded from DB).

Usage:
    python run_scoring_simulation.py

Configuration:
    - DB connection via env vars: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
    - Custom evaluations in mock_evaluations.py
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


def make_aware(dt: datetime) -> datetime:
    """Convert naive datetime to UTC-aware. Returns None if input is None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from dotenv import load_dotenv
validator_env = Path(__file__).parent.parent.parent / '.env'
load_dotenv(validator_env)

import bittensor as bt

# Enable bittensor logging to console
bt.logging.set_debug(True)

from gittensor.classes import FileChange, Issue, MinerEvaluation, PRState, PullRequest
from gittensor.validator.configurations.tier_config import TIERS, TierStats
from gittensor.validator.evaluation.dynamic_emissions import apply_dynamic_emissions_using_network_contributions
from gittensor.validator.evaluation.inspections import detect_and_penalize_miners_sharing_github
from gittensor.validator.evaluation.normalize import normalize_rewards_linear
from gittensor.validator.evaluation.scoring import finalize_miner_scores, score_miner_prs
from gittensor.validator.utils.load_weights import load_master_repo_weights, load_programming_language_weights

try:
    from gittensor.validator.test.simulation.mock_evaluations import get_custom_evaluations
    CUSTOM_EVALUATIONS_AVAILABLE = True
except ImportError:
    CUSTOM_EVALUATIONS_AVAILABLE = False


# =============================================================================
# Database Queries
# =============================================================================

LOAD_ALL_MINERS = """
SELECT DISTINCT m.uid, m.hotkey, m.github_id
FROM miners m
INNER JOIN pull_requests pr ON m.uid = pr.uid AND m.hotkey = pr.hotkey AND m.github_id = pr.github_id
ORDER BY m.uid
"""

LOAD_PULL_REQUESTS_FOR_MINER = """
SELECT number, repository_full_name, uid, hotkey, github_id, title, author_login,
       merged_at, pr_created_at, pr_state, additions, deletions, commits,
       total_lines_scored, gittensor_tagged, merged_by_login, description, last_edited_at
FROM pull_requests
WHERE uid = %s AND hotkey = %s AND github_id = %s
ORDER BY merged_at DESC NULLS LAST
"""

LOAD_FILE_CHANGES_FOR_PR = """
SELECT pr_number, repository_full_name, filename, changes, additions, deletions, status, patch, file_extension
FROM file_changes WHERE pr_number = %s AND repository_full_name = %s
"""

LOAD_ISSUES_FOR_PR = """
SELECT number, pr_number, repository_full_name, title, created_at, closed_at
FROM issues WHERE pr_number = %s AND repository_full_name = %s
"""


def create_db_connection():
    """Create database connection from env vars."""
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 not installed")
        return None

    try:
        conn = psycopg2.connect(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', 5432)),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASSWORD', ''),
            database=os.getenv('DB_NAME', 'gittensor_validator'),
        )
        conn.autocommit = False
        print(f"Connected to {os.getenv('DB_NAME', 'gittensor_validator')}")
        return conn
    except Exception as e:
        print(f"ERROR: DB connection failed: {e}")
        return None


# =============================================================================
# Data Loading & Serialization
# =============================================================================

def load_file_changes(conn, pr_number: int, repo: str) -> List[FileChange]:
    """Load file changes for a PR from database."""
    cur = conn.cursor()
    cur.execute(LOAD_FILE_CHANGES_FOR_PR, (pr_number, repo))
    rows = cur.fetchall()
    cur.close()
    return [FileChange(
        pr_number=r[0], repository_full_name=r[1], filename=r[2], changes=r[3],
        additions=r[4], deletions=r[5], status=r[6], patch=r[7], file_extension=r[8]
    ) for r in rows]


def load_issues(conn, pr_number: int, repo: str, pr_author: str) -> List[Issue]:
    """Load issues for a PR. Sets defaults for author_login/state (not in DB schema)."""
    cur = conn.cursor()
    cur.execute(LOAD_ISSUES_FOR_PR, (pr_number, repo))
    rows = cur.fetchall()
    cur.close()
    issue_author = "external_user" if pr_author != "external_user" else "other_user"
    return [Issue(
        number=r[0], pr_number=r[1], repository_full_name=r[2], title=r[3],
        created_at=make_aware(r[4]), closed_at=make_aware(r[5]),
        author_login=issue_author, state='CLOSED'
    ) for r in rows]


def load_pr_from_row(conn, row: tuple) -> PullRequest:
    """Convert DB row to PullRequest with file_changes and issues pre-loaded."""
    # Lowercase repo name to match master_repositories keys
    repo_full_name = row[1].lower() if row[1] else row[1]
    pr = PullRequest(
        number=row[0], repository_full_name=repo_full_name, uid=row[2], hotkey=row[3],
        github_id=row[4], title=row[5], author_login=row[6],
        merged_at=make_aware(row[7]), created_at=make_aware(row[8]),
        pr_state=PRState(row[9]), additions=row[10] or 0,
        deletions=row[11] or 0, commits=row[12] or 0, total_lines_scored=row[13] or 0,
        gittensor_tagged=row[14] or False, merged_by_login=row[15],
        description=row[16], last_edited_at=make_aware(row[17]),
    )
    # Pre-load file changes so score_miner_prs skips GitHub API call (use original row[1] for DB query)
    file_changes = load_file_changes(conn, pr.number, row[1])
    if file_changes:
        pr.set_file_changes(file_changes)
    # Pre-load issues
    issues = load_issues(conn, pr.number, row[1], pr.author_login)
    if issues:
        pr.issues = issues
    return pr


def load_miner_evaluation(conn, uid: int, hotkey: str, github_id: str) -> MinerEvaluation:
    """Build MinerEvaluation from database with all PRs pre-loaded."""
    miner_eval = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id)
    for tier in TIERS.keys():
        miner_eval.stats_by_tier[tier] = TierStats()

    cur = conn.cursor()
    cur.execute(LOAD_PULL_REQUESTS_FOR_MINER, (uid, hotkey, github_id))
    rows = cur.fetchall()
    cur.close()

    for row in rows:
        pr = load_pr_from_row(conn, row)
        if pr.pr_state == PRState.MERGED:
            miner_eval.merged_pull_requests.append(pr)
            miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)
        elif pr.pr_state == PRState.OPEN:
            miner_eval.open_pull_requests.append(pr)
        elif pr.pr_state == PRState.CLOSED:
            miner_eval.closed_pull_requests.append(pr)

    return miner_eval


def load_all_evaluations(conn) -> Dict[int, MinerEvaluation]:
    """Load all miner evaluations from database (only miners with PRs)."""
    cur = conn.cursor()
    cur.execute(LOAD_ALL_MINERS)
    miners = cur.fetchall()
    cur.close()

    evaluations = {}
    for uid, hotkey, github_id in miners:
        evaluations[uid] = load_miner_evaluation(conn, uid, hotkey, github_id)
        e = evaluations[uid]
        print(f"  uid={uid} ({github_id}): {e.total_merged_prs} merged, {e.total_open_prs} open, {e.total_closed_prs} closed")

    print(f"Loaded {len(evaluations)} miners with PRs")
    return evaluations


# =============================================================================
# Main Simulation
# =============================================================================

def run_scoring_simulation(include_custom: bool = True) -> Tuple[Dict[int, MinerEvaluation], Dict[int, float], Dict[int, float]]:
    """Run full scoring pipeline on DB data."""
    print("=" * 70)
    print("SCORING SIMULATION START")
    print("=" * 70)
    sys.stdout.flush(); time.sleep(0.1)

    # 1. Connect to DB
    print("\n[1/8] Connecting to DB...")
    sys.stdout.flush(); time.sleep(0.1)
    conn = create_db_connection()
    if not conn:
        return {}, {}, {}

    # 2. Load weights
    print("\n[2/8] Loading weights...")
    sys.stdout.flush(); time.sleep(0.1)
    master_repos = load_master_repo_weights()
    prog_langs = load_programming_language_weights()
    print(f"  {len(master_repos)} repos, {len(prog_langs)} languages")
    sys.stdout.flush(); time.sleep(0.1)

    # 3. Load evaluations from DB
    print("\n[3/8] Loading evaluations from DB...")
    sys.stdout.flush(); time.sleep(0.1)
    evals = load_all_evaluations(conn)

    # 4. Add custom evaluations
    if include_custom and CUSTOM_EVALUATIONS_AVAILABLE:
        print("\n[4/8] Adding custom evaluations...")
        for uid, ev in get_custom_evaluations().items():
            evals[uid] = ev
            print(f"  Added custom uid={uid}")
    else:
        print("\n[4/8] No custom evaluations.")
    sys.stdout.flush(); time.sleep(0.1)

    # 5. Score PRs (uses original score_miner_prs - skips GitHub call since file_changes pre-loaded)
    print("\n[5/8] Scoring PRs...")
    sys.stdout.flush(); time.sleep(0.1)
    for uid, ev in evals.items():
        if ev.failed_reason:
            continue
        score_miner_prs(ev, master_repos, prog_langs)

    # 6. Detect duplicate GitHub accounts
    print("\n[6/8] Checking for duplicate GitHub accounts...")
    sys.stdout.flush(); time.sleep(0.1)
    detect_and_penalize_miners_sharing_github(evals)

    # 7. Finalize scores
    print("\n[7/8] Finalizing scores...")
    sys.stdout.flush(); time.sleep(0.1)
    finalize_miner_scores(evals)

    # 8. Normalize & apply dynamic emissions
    print("\n[8/8] Normalizing & applying dynamic emissions...")
    sys.stdout.flush(); time.sleep(0.1)
    normalized = normalize_rewards_linear(evals)
    scaled = apply_dynamic_emissions_using_network_contributions(normalized, evals)

    conn.close()

    # Print summary
    _print_summary(evals, normalized, scaled)

    print("\n" + "=" * 70)
    print("SCORING SIMULATION COMPLETE")
    print("=" * 70)

    return evals, normalized, scaled


def _print_summary(evals: Dict[int, MinerEvaluation], normalized: Dict[int, float], scaled: Dict[int, float]):
    """Print results summary."""
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    sorted_uids = sorted(scaled.keys(), key=lambda u: scaled.get(u, 0), reverse=True)

    print(f"\n{'UID':<6} {'GitHub':<18} {'Tier':<7} {'Merged':<7} {'Score':<10} {'Normalized':<12} {'Scaled':<10}")
    print("-" * 82)

    for uid in sorted_uids:
        ev = evals.get(uid)
        if not ev:
            continue
        tier = ev.current_tier.value if ev.current_tier else "None"
        print(
            f"{uid:<6} {(ev.github_id or 'N/A'):<18} {tier:<7} {ev.total_merged_prs:<7} "
            f"{ev.total_score:<10.2f} {normalized.get(uid, 0):<12.6f} {scaled.get(uid, 0):<10.6f}"
        )


if __name__ == "__main__":
    try:
        run_scoring_simulation()
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
