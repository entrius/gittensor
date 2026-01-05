# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Scoring Simulation Runner

Runs the full incentive mechanism scoring pipeline:
1. Loads miner identities (uid, hotkey, github_id) from the test database
2. Fetches PRs from GitHub in real-time using the production load_miners_prs function
3. Scores PRs using token-based AST scoring with file contents from GitHub

This tests the complete production flow including GitHub API integration.

Usage:
    python run_scoring_simulation.py

Configuration:
    - DB connection via env vars: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
    - GitHub PAT via GITHUB_PAT or GITHUB_TOKEN env var (required)
    - Custom evaluations in mock_evaluations.py
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import bittensor as bt
from dotenv import load_dotenv

from gittensor.classes import MinerEvaluation
from gittensor.utils.github_api_tools import load_miners_prs
from gittensor.validator.configurations.tier_config import TIERS, TierStats
from gittensor.validator.evaluation.dynamic_emissions import apply_dynamic_emissions_using_network_contributions
from gittensor.validator.evaluation.inspections import detect_and_penalize_miners_sharing_github
from gittensor.validator.evaluation.normalize import normalize_rewards_linear
from gittensor.validator.evaluation.scoring import finalize_miner_scores, score_miner_prs
from gittensor.validator.utils.load_weights import (
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_weights,
)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))


validator_env = Path(__file__).parent.parent.parent / '.env'
load_dotenv(validator_env)


# Enable bittensor logging to console
bt.logging.set_debug(True)

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


def create_db_connection():
    """Create database connection from env vars."""
    try:
        import psycopg2
    except ImportError:
        print('ERROR: psycopg2 not installed')
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
        print(f'Connected to {os.getenv("DB_NAME", "gittensor_validator")}')
        return conn
    except Exception as e:
        print(f'ERROR: DB connection failed: {e}')
        return None


def load_miner_identities(conn) -> List[Tuple[int, str, str]]:
    """Load miner identities (uid, hotkey, github_id) from database."""
    cur = conn.cursor()
    cur.execute(LOAD_ALL_MINERS)
    miners = cur.fetchall()
    cur.close()
    print(f'  Found {len(miners)} miners with PRs in DB')
    return miners


def create_miner_evaluation(uid: int, hotkey: str, github_id: str, github_pat: str) -> MinerEvaluation:
    """Create a MinerEvaluation with identity info and GitHub PAT."""
    miner_eval = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id, github_pat=github_pat)
    for tier in TIERS.keys():
        miner_eval.stats_by_tier[tier] = TierStats()
    return miner_eval


# =============================================================================
# Main Simulation
# =============================================================================


def run_scoring_simulation(
    include_custom: bool = True,
) -> Tuple[Dict[int, MinerEvaluation], Dict[int, float], Dict[int, float]]:
    """Run full scoring pipeline fetching PRs from GitHub in real-time."""
    print('=' * 70)
    print('SCORING SIMULATION START')
    print('=' * 70)
    sys.stdout.flush()
    time.sleep(0.1)

    # 1. Load weights and GitHub PAT
    print('\n[1/8] Loading weights and configuration...')
    sys.stdout.flush()
    time.sleep(0.1)
    master_repos = load_master_repo_weights()
    prog_langs = load_programming_language_weights()
    token_weights = load_token_weights()
    github_pat = os.getenv('GITHUB_PAT') or os.getenv('GITHUB_TOKEN')
    print(f'  {len(master_repos)} repos, {len(prog_langs)} languages')
    print(
        f'  Token weights: {len(token_weights.structural_bonus)} structural, {len(token_weights.leaf_tokens)} leaf types'
    )
    if not github_pat:
        print('  ERROR: No GitHub PAT set - cannot fetch PRs from GitHub')
        return {}, {}, {}
    print('  GitHub PAT: Available')
    sys.stdout.flush()
    time.sleep(0.1)

    # 2. Connect to DB to get miner identities
    print('\n[2/8] Loading miner identities from DB...')
    sys.stdout.flush()
    time.sleep(0.1)
    conn = create_db_connection()
    if not conn:
        return {}, {}, {}
    miners = load_miner_identities(conn)
    conn.close()

    # 3. Create evaluations and fetch PRs from GitHub
    print('\n[3/8] Fetching PRs from GitHub for each miner...')
    sys.stdout.flush()
    time.sleep(0.1)
    evals: Dict[int, MinerEvaluation] = {}
    for uid, hotkey, github_id in miners:
        print(f'  Fetching PRs for uid={uid} ({github_id})...')
        ev = create_miner_evaluation(uid, hotkey, github_id, github_pat)
        load_miners_prs(ev, master_repos, max_prs=100)
        evals[uid] = ev
        print(f'    -> {ev.total_merged_prs} merged, {ev.total_open_prs} open, {ev.total_closed_prs} closed')

    # 4. Add custom evaluations
    if include_custom and CUSTOM_EVALUATIONS_AVAILABLE:
        print('\n[4/8] Adding custom evaluations...')
        for uid, ev in get_custom_evaluations().items():
            ev.github_pat = github_pat
            evals[uid] = ev
            print(f'  Added custom uid={uid}')
    else:
        print('\n[4/8] No custom evaluations.')
    sys.stdout.flush()
    time.sleep(0.1)

    # 5. Score PRs (uses token-based scoring with file contents from GitHub)
    print('\n[5/8] Scoring PRs with token-based scoring...')
    sys.stdout.flush()
    time.sleep(0.1)
    for uid, ev in evals.items():
        if ev.failed_reason:
            continue
        score_miner_prs(ev, master_repos, prog_langs, token_weights)

    # 6. Detect duplicate GitHub accounts
    print('\n[6/8] Checking for duplicate GitHub accounts...')
    sys.stdout.flush()
    time.sleep(0.1)
    detect_and_penalize_miners_sharing_github(evals)

    # 7. Finalize scores
    print('\n[7/8] Finalizing scores...')
    sys.stdout.flush()
    time.sleep(0.1)
    finalize_miner_scores(evals)

    # 8. Normalize & apply dynamic emissions
    print('\n[8/8] Normalizing & applying dynamic emissions...')
    sys.stdout.flush()
    time.sleep(0.1)
    normalized = normalize_rewards_linear(evals)
    scaled = apply_dynamic_emissions_using_network_contributions(normalized, evals)

    # Print summary
    _print_summary(evals, normalized, scaled)

    print('\n' + '=' * 70)
    print('SCORING SIMULATION COMPLETE')
    print('=' * 70)

    return evals, normalized, scaled


def _print_summary(evals: Dict[int, MinerEvaluation], normalized: Dict[int, float], scaled: Dict[int, float]):
    """Print results summary."""
    print('\n' + '=' * 70)
    print('RESULTS SUMMARY')
    print('=' * 70)

    sorted_uids = sorted(scaled.keys(), key=lambda u: scaled.get(u, 0), reverse=True)

    print(f'\n{"UID":<6} {"GitHub":<18} {"Tier":<7} {"Merged":<7} {"Score":<10} {"Normalized":<12} {"Scaled":<10}')
    print('-' * 82)

    for uid in sorted_uids:
        ev = evals.get(uid)
        if not ev:
            continue
        tier = ev.current_tier.value if ev.current_tier else 'None'
        print(
            f'{uid:<6} {(ev.github_id or "N/A"):<18} {tier:<7} {ev.total_merged_prs:<7} '
            f'{ev.total_score:<10.2f} {normalized.get(uid, 0):<12.6f} {scaled.get(uid, 0):<10.6f}'
        )


if __name__ == '__main__':
    try:
        run_scoring_simulation()
    except KeyboardInterrupt:
        print('\nInterrupted')
        sys.exit(0)
    except Exception as e:
        print(f'ERROR: {e}')
        import traceback

        traceback.print_exc()
        sys.exit(1)
