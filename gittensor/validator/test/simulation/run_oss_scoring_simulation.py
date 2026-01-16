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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bittensor as bt
from dotenv import load_dotenv

from gittensor.constants import MAX_CODE_DENSITY_MULTIPLIER, MIN_TOKEN_SCORE_FOR_BASE_SCORE

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
    load_token_config,
)
from gittensor.validator.utils.storage import DatabaseStorage


# =============================================================================
# Comparison Data Structures
# =============================================================================


@dataclass
class PRComparison:
    """Tracks before/after scores for a single PR"""

    repo: str
    number: int
    uid: int
    github_id: str
    pr_state: str = 'MERGED'
    # Before (from DB)
    before_earned: float = 0.0
    before_base: float = 0.0
    before_token: float = 0.0
    before_low_value: bool = False
    # After (from simulation)
    after_earned: float = 0.0
    after_base: float = 0.0
    after_token: float = 0.0
    after_low_value: bool = False  # token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE
    # Computed
    code_density: float = 0.0  # token_score / total_lines
    total_lines: int = 0
    contribution_bonus: float = 0.0

    @property
    def url(self) -> str:
        return f'https://github.com/{self.repo}/pull/{self.number}'

    @property
    def earned_delta(self) -> float:
        return self.after_earned - self.before_earned

    @property
    def earned_pct_change(self) -> float:
        if self.before_earned == 0:
            return 100.0 if self.after_earned > 0 else 0.0
        return (self.earned_delta / self.before_earned) * 100


@dataclass
class MinerComparison:
    """Tracks before/after scores for a miner"""

    uid: int
    github_id: str
    before_total: float = 0.0
    after_total: float = 0.0
    before_rank: int = 0
    after_rank: int = 0

    @property
    def rank_change(self) -> int:
        return self.before_rank - self.after_rank  # Positive = improved

    @property
    def score_delta(self) -> float:
        return self.after_total - self.before_total

    @property
    def score_pct_change(self) -> float:
        if self.before_total == 0:
            return 100.0 if self.after_total > 0 else 0.0
        return (self.score_delta / self.before_total) * 100


def make_aware(dt: datetime) -> datetime:
    """Convert naive datetime to UTC-aware. Returns None if input is None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))


validator_env = Path(__file__).parent.parent.parent / '.env'
load_dotenv(validator_env)


# Enable bittensor logging to console
bt.logging.set_debug(True)

# Set to an integer to limit the number of miners to score (e.g., 2, 5, 10)
# Set to None to score all miners
MINER_LIMIT = None

# Set to empty list [] to score all miners (subject to MINER_LIMIT)
SPECIFIC_GITHUB_IDS = []


try:
    from gittensor.validator.test.simulation.mock_evaluations import get_custom_evaluations

    CUSTOM_EVALUATIONS_AVAILABLE = True
except ImportError:
    CUSTOM_EVALUATIONS_AVAILABLE = False


# =============================================================================
# Database Queries
# =============================================================================

LOAD_ALL_MINERS = """
SELECT DISTINCT me.uid, me.hotkey, me.github_id, me.total_score
FROM miner_evaluations me
WHERE me.total_score > 0 OR EXISTS (
    SELECT 1 FROM pull_requests pr
    WHERE pr.uid = me.uid AND pr.github_id = me.github_id
)
ORDER BY me.total_score DESC
"""

LOAD_PR_SCORES = """
SELECT
    pr.repository_full_name,
    pr.number,
    pr.pr_state,
    pr.earned_score,
    pr.base_score,
    pr.token_score,
    pr.low_value_pr,
    pr.additions,
    pr.deletions,
    pr.uid,
    pr.github_id
FROM pull_requests pr
WHERE pr.uid = %s AND pr.github_id = %s
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
    """Load miner identities (uid, hotkey, github_id) from database.

    Applies filters based on SPECIFIC_GITHUB_IDS and MINER_LIMIT configuration.
    If SPECIFIC_GITHUB_IDS are provided, bypasses DB entirely and creates mock identities
    to fetch PRs directly from GitHub GraphQL.
    """
    # If specific github_ids are provided, bypass DB and create mock identities directly
    # This allows fetching fresh PRs from GraphQL without requiring DB records
    if SPECIFIC_GITHUB_IDS:
        miners = []
        print(f'  Using SPECIFIC_GITHUB_IDS (bypassing DB): {SPECIFIC_GITHUB_IDS}')
        for i, github_id in enumerate(SPECIFIC_GITHUB_IDS):
            mock_uid = -(i + 1)
            mock_hotkey = f'mock_hotkey_{github_id}'
            # Include dummy total_score (0.0) to match DB query result format
            miners.append((mock_uid, mock_hotkey, github_id, 0.0))
            print(f'    -> Mock miner: uid={mock_uid}, github_id={github_id}')

        # Apply miner limit if configured
        if MINER_LIMIT is not None and len(miners) > MINER_LIMIT:
            miners = miners[:MINER_LIMIT]
            print(f'  Limited to {len(miners)} miners by MINER_LIMIT')

        return miners

    # Otherwise, load from database
    cur = conn.cursor()
    cur.execute(LOAD_ALL_MINERS)
    miners = cur.fetchall()
    cur.close()
    total_in_db = len(miners)
    print(f'  Found {total_in_db} miners with PRs in DB')

    # Apply miner limit if configured
    if MINER_LIMIT is not None and len(miners) > MINER_LIMIT:
        miners = miners[:MINER_LIMIT]
        print(f'  Limited to {len(miners)} miners by MINER_LIMIT')

    return miners


def create_miner_evaluation(uid: int, hotkey: str, github_id: str, github_pat: str) -> MinerEvaluation:
    """Create a MinerEvaluation with identity info and GitHub PAT."""
    # Ensure github_id is a string (may be int from config or DB)
    github_id_str = str(github_id) if github_id else '0'
    miner_eval = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id_str, github_pat=github_pat)
    for tier in TIERS.keys():
        miner_eval.stats_by_tier[tier] = TierStats()
    return miner_eval


# =============================================================================
# Comparison Helper Functions
# =============================================================================


def load_before_pr_scores(conn, miners: List[Tuple]) -> Dict[Tuple[str, int], dict]:
    """Load existing PR scores from database for comparison.

    Returns dict keyed by (repo, pr_number) with score fields.
    """
    before_scores = {}
    cur = conn.cursor()

    for uid, _, github_id, _ in miners:
        cur.execute(LOAD_PR_SCORES, (uid, github_id))
        rows = cur.fetchall()
        for row in rows:
            repo, number, pr_state, earned, base, token, low_value, adds, dels, _, _ = row
            key = (repo, number)
            before_scores[key] = {
                'pr_state': pr_state,
                'earned_score': float(earned) if earned else 0.0,
                'base_score': float(base) if base else 0.0,
                'token_score': float(token) if token else 0.0,
                'low_value_pr': bool(low_value) if low_value is not None else False,
                'additions': int(adds) if adds else 0,
                'deletions': int(dels) if dels else 0,
            }

    cur.close()
    return before_scores


def build_pr_comparisons(
    evals: Dict[int, MinerEvaluation], before_scores: Dict[Tuple[str, int], dict]
) -> List[PRComparison]:
    """Build PR comparison objects from evaluations and DB scores."""
    comparisons = []

    for uid, ev in evals.items():
        all_prs = ev.merged_pull_requests + ev.open_pull_requests + ev.closed_pull_requests

        for pr in all_prs:
            key = (pr.repository_full_name, pr.number)
            before = before_scores.get(key, {})

            # Calculate code density
            total_lines = pr.additions + pr.deletions
            code_density = 0.0
            if total_lines > 0 and pr.token_score > 0:
                code_density = min(pr.token_score / total_lines, MAX_CODE_DENSITY_MULTIPLIER)

            # Calculate contribution bonus (base_score - initial_base_score)
            # initial_base_score = 30 * code_density (when token_score >= 5)
            initial_base = 30.0 * code_density if pr.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE else 0.0
            contribution_bonus = max(0.0, pr.base_score - initial_base)

            comp = PRComparison(
                repo=pr.repository_full_name,
                number=pr.number,
                uid=uid,
                github_id=ev.github_id,
                pr_state=pr.pr_state.value,
                # Before
                before_earned=before.get('earned_score', 0.0),
                before_base=before.get('base_score', 0.0),
                before_token=before.get('token_score', 0.0),
                before_low_value=before.get('low_value_pr', False),
                # After
                after_earned=pr.earned_score,
                after_base=pr.base_score,
                after_token=pr.token_score,
                after_low_value=pr.token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE,
                # Computed
                code_density=code_density,
                total_lines=total_lines,
                contribution_bonus=contribution_bonus,
            )
            comparisons.append(comp)

    return comparisons


def build_miner_comparisons(
    evals: Dict[int, MinerEvaluation], before_miner_scores: Dict[int, float]
) -> List[MinerComparison]:
    """Build miner comparison objects with rankings."""
    comparisons = []

    # Build list of (uid, github_id, before, after)
    data = []
    for uid, ev in evals.items():
        before = before_miner_scores.get(uid, 0.0)
        after = ev.total_score
        data.append((uid, ev.github_id, before, after))

    # Calculate before rankings (non-zero only)
    before_ranked = sorted([(u, g, b, a) for u, g, b, a in data if b > 0], key=lambda x: -x[2])
    before_ranks = {u: i + 1 for i, (u, _, _, _) in enumerate(before_ranked)}

    # Calculate after rankings (non-zero only)
    after_ranked = sorted([(u, g, b, a) for u, g, b, a in data if a > 0], key=lambda x: -x[3])
    after_ranks = {u: i + 1 for i, (u, _, _, _) in enumerate(after_ranked)}

    for uid, github_id, before, after in data:
        comp = MinerComparison(
            uid=uid,
            github_id=github_id,
            before_total=before,
            after_total=after,
            before_rank=before_ranks.get(uid, 0),
            after_rank=after_ranks.get(uid, 0),
        )
        comparisons.append(comp)

    return comparisons


def print_analysis_report(
    evals: Dict[int, MinerEvaluation],
    pr_comparisons: List[PRComparison],
    miner_comparisons: List[MinerComparison],
    normalized: Dict[int, float],
    scaled: Dict[int, float],
) -> None:
    """Print comprehensive analysis report comparing before/after scores."""
    print('\n' + '=' * 70)
    print('TOKEN SCORING ANALYSIS REPORT')
    print('=' * 70)

    # Section 1: Miner Score Comparison
    _print_miner_score_comparison(miner_comparisons)

    # Section 2: Ranking Changes
    _print_ranking_changes(miner_comparisons)

    # Section 3: Low Value PR Analysis
    _print_low_value_analysis(pr_comparisons)

    # Section 4: Notable PRs
    _print_notable_prs(pr_comparisons)

    # Section 5: Biggest Score Changes
    _print_biggest_changes(pr_comparisons)

    # Section 6: Summary Statistics
    _print_summary_stats(evals, pr_comparisons, miner_comparisons, normalized, scaled)


def _print_miner_score_comparison(comparisons: List[MinerComparison]) -> None:
    """Print Section 1: Miner score comparison table."""
    print('\n[1/6] MINER SCORE COMPARISON')
    print('-' * 70)

    # Filter to miners with non-zero before OR after
    active = [c for c in comparisons if c.before_total > 0 or c.after_total > 0]
    # Sort by absolute delta descending
    active.sort(key=lambda c: abs(c.score_delta), reverse=True)

    print(f'{"UID":<6} {"GitHub":<18} {"Before":>12} {"After":>12} {"Delta":>12} {"%Change":>10}')
    print('-' * 70)

    for c in active:
        delta_str = f'{c.score_delta:+.2f}' if c.score_delta != 0 else '0.00'
        pct_str = f'{c.score_pct_change:+.1f}%' if c.before_total > 0 else 'N/A'
        print(f'{c.uid:<6} {c.github_id:<18} {c.before_total:>12.2f} {c.after_total:>12.2f} {delta_str:>12} {pct_str:>10}')


def _print_ranking_changes(comparisons: List[MinerComparison]) -> None:
    """Print Section 2: Ranking changes for non-zero miners."""
    print('\n[2/6] RANKING CHANGES (Non-Zero Miners)')
    print('-' * 70)

    # Only include miners with non-zero after score
    ranked = [c for c in comparisons if c.after_total > 0]
    # Sort by after_rank
    ranked.sort(key=lambda c: c.after_rank)

    print(f'{"Rank":<6} {"UID":<6} {"GitHub":<18} {"Before -> After":<18} {"Change":>10}')
    print('-' * 70)

    for c in ranked[:15]:  # Top 15
        before_str = f'#{c.before_rank}' if c.before_rank > 0 else 'NEW'
        change_str = ''
        if c.before_rank > 0 and c.after_rank > 0:
            if c.rank_change > 0:
                change_str = f'+{c.rank_change}'
            elif c.rank_change < 0:
                change_str = f'{c.rank_change}'
            else:
                change_str = '0'
        print(f'{c.after_rank:<6} {c.uid:<6} {c.github_id:<18} {before_str + " -> #" + str(c.after_rank):<18} {change_str:>10}')


def _print_low_value_analysis(pr_comparisons: List[PRComparison]) -> None:
    """Print Section 3: Low value PR analysis."""
    print('\n[3/6] LOW VALUE PR ANALYSIS')
    print('-' * 70)

    # Only count merged PRs for low value analysis
    merged_prs = [p for p in pr_comparisons if p.pr_state == 'MERGED']
    total_merged = len(merged_prs)

    before_low_value = sum(1 for p in merged_prs if p.before_low_value)
    after_low_value = sum(1 for p in merged_prs if p.after_low_value)

    # PRs not earning base score (token_score < 5)
    no_base_score = sum(1 for p in merged_prs if p.after_token < MIN_TOKEN_SCORE_FOR_BASE_SCORE)

    print(f'PRs Not Earning Base Score (token_score < {MIN_TOKEN_SCORE_FOR_BASE_SCORE}):')
    print(f'  Before (DB low_value_pr):  {before_low_value} PRs')
    print(f'  After (token < {MIN_TOKEN_SCORE_FOR_BASE_SCORE}):       {no_base_score} PRs')
    if before_low_value > 0:
        change_pct = ((no_base_score - before_low_value) / before_low_value) * 100
        print(f'  Change:                    {no_base_score - before_low_value:+d} PRs ({change_pct:+.1f}%)')
    print(f'\n  Total Merged PRs:          {total_merged}')
    if total_merged > 0:
        print(f'  % Not Earning Base:        {(no_base_score / total_merged) * 100:.1f}%')


def _print_notable_prs(pr_comparisons: List[PRComparison]) -> None:
    """Print Section 4: Notable PRs with GitHub links."""
    print('\n[4/6] NOTABLE PRs')
    print('-' * 70)

    # Only consider merged PRs with positive earned score
    merged = [p for p in pr_comparisons if p.pr_state == 'MERGED']

    # Highest earning PR
    if merged:
        highest_earning = max(merged, key=lambda p: p.after_earned)
        print(f'HIGHEST EARNING PR:')
        print(f'  URL: {highest_earning.url}')
        print(f'  UID: {highest_earning.uid} | Earned: {highest_earning.after_earned:,.2f} | Token: {highest_earning.after_token:.1f} | Density: {highest_earning.code_density:.2f}')

    # Highest contribution bonus
    with_bonus = [p for p in merged if p.contribution_bonus > 0]
    if with_bonus:
        highest_bonus = max(with_bonus, key=lambda p: p.contribution_bonus)
        print(f'\nHIGHEST CONTRIBUTION BONUS:')
        print(f'  URL: {highest_bonus.url}')
        print(f'  UID: {highest_bonus.uid} | Bonus: {highest_bonus.contribution_bonus:.2f} | Token Score: {highest_bonus.after_token:.1f}')

    # Closest to base threshold (token_score just above 5)
    above_threshold = [p for p in merged if p.after_token >= MIN_TOKEN_SCORE_FOR_BASE_SCORE]
    if above_threshold:
        closest = min(above_threshold, key=lambda p: p.after_token - MIN_TOKEN_SCORE_FOR_BASE_SCORE)
        print(f'\nCLOSEST TO BASE THRESHOLD:')
        print(f'  URL: {closest.url}')
        print(f'  UID: {closest.uid} | Token: {closest.after_token:.2f} (just above {MIN_TOKEN_SCORE_FOR_BASE_SCORE} threshold)')

    # Highest code density
    with_density = [p for p in merged if p.code_density > 0]
    if with_density:
        highest_density = max(with_density, key=lambda p: p.code_density)
        print(f'\nHIGHEST CODE DENSITY:')
        print(f'  URL: {highest_density.url}')
        capped_note = ' (capped)' if highest_density.code_density >= MAX_CODE_DENSITY_MULTIPLIER else ''
        print(f'  UID: {highest_density.uid} | Density: {highest_density.code_density:.2f}{capped_note} | Token: {highest_density.after_token:.1f} / Lines: {highest_density.total_lines}')

    # Lowest code density (non-zero, earned score > 0)
    with_low_density = [p for p in merged if p.code_density > 0 and p.after_earned > 0]
    if with_low_density:
        lowest_density = min(with_low_density, key=lambda p: p.code_density)
        print(f'\nLOWEST CODE DENSITY (earning score):')
        print(f'  URL: {lowest_density.url}')
        print(f'  UID: {lowest_density.uid} | Density: {lowest_density.code_density:.2f} | Token: {lowest_density.after_token:.1f} / Lines: {lowest_density.total_lines}')


def _print_biggest_changes(pr_comparisons: List[PRComparison]) -> None:
    """Print Section 5: Biggest PR score changes."""
    print('\n[5/6] BIGGEST PR SCORE CHANGES (Top 5)')
    print('-' * 70)

    # Only merged PRs with both before and after data
    merged = [p for p in pr_comparisons if p.pr_state == 'MERGED']

    # Top increases
    increases = sorted([p for p in merged if p.earned_delta > 0], key=lambda p: -p.earned_delta)[:5]
    if increases:
        print('INCREASES:')
        for i, p in enumerate(increases, 1):
            print(f'  #{i}: {p.url}')
            print(f'      Before: {p.before_earned:.2f} -> After: {p.after_earned:.2f} ({p.earned_delta:+.2f}, {p.earned_pct_change:+.1f}%)')

    # Top decreases
    decreases = sorted([p for p in merged if p.earned_delta < 0], key=lambda p: p.earned_delta)[:5]
    if decreases:
        print('\nDECREASES:')
        for i, p in enumerate(decreases, 1):
            print(f'  #{i}: {p.url}')
            print(f'      Before: {p.before_earned:.2f} -> After: {p.after_earned:.2f} ({p.earned_delta:+.2f}, {p.earned_pct_change:+.1f}%)')


def _print_summary_stats(
    evals: Dict[int, MinerEvaluation],
    pr_comparisons: List[PRComparison],
    miner_comparisons: List[MinerComparison],
    normalized: Dict[int, float],
    scaled: Dict[int, float],
) -> None:
    """Print Section 6: Summary statistics."""
    print('\n[6/6] SUMMARY STATISTICS')
    print('-' * 70)

    total_miners = len(evals)
    total_prs = len(pr_comparisons)
    merged_prs = sum(1 for p in pr_comparisons if p.pr_state == 'MERGED')

    # Miners with score changes
    active_miners = [c for c in miner_comparisons if c.before_total > 0 or c.after_total > 0]
    increased = sum(1 for c in active_miners if c.score_delta > 0)
    decreased = sum(1 for c in active_miners if c.score_delta < 0)
    unchanged = sum(1 for c in active_miners if c.score_delta == 0)

    # Total scores
    total_before = sum(c.before_total for c in miner_comparisons)
    total_after = sum(c.after_total for c in miner_comparisons)
    total_change = total_after - total_before
    total_change_pct = (total_change / total_before * 100) if total_before > 0 else 0

    # Average change per miner
    avg_change = 0.0
    miners_with_before = [c for c in miner_comparisons if c.before_total > 0]
    if miners_with_before:
        avg_change = sum(c.score_pct_change for c in miners_with_before) / len(miners_with_before)

    print(f'Total Miners:                {total_miners}')
    print(f'Total PRs Scored:            {total_prs} ({merged_prs} merged)')
    print(f'Miners with Score Increase:  {increased} ({increased / len(active_miners) * 100:.1f}%)' if active_miners else '')
    print(f'Miners with Score Decrease:  {decreased} ({decreased / len(active_miners) * 100:.1f}%)' if active_miners else '')
    print(f'Miners Unchanged:            {unchanged}')
    print(f'Avg Miner Score Change:      {avg_change:+.1f}%' if miners_with_before else '')
    print(f'Total Score Before:          {total_before:,.2f}')
    print(f'Total Score After:           {total_after:,.2f} ({total_change:+,.2f}, {total_change_pct:+.1f}%)')

    print('\n' + '=' * 70)


# =============================================================================
# Main Simulation
# =============================================================================


def run_scoring_simulation(
    include_custom: bool = True,
    store_evaluations: bool = False,
) -> Tuple[Dict[int, MinerEvaluation], Dict[int, float], Dict[int, float]]:
    """Run full scoring pipeline on DB data.

    Args:
        include_custom: Include custom evaluations from mock_evaluations.py
        store_evaluations: If True, store evaluations to database via DatabaseStorage
    """
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
    token_config = load_token_config()
    github_pat = os.getenv('GITHUB_PAT') or os.getenv('GITHUB_TOKEN')
    print(f'  {len(master_repos)} repos, {len(prog_langs)} languages')
    print(
        f'  Token config: {len(token_config.structural_bonus)} structural, {len(token_config.leaf_tokens)} leaf types'
    )
    if not github_pat:
        print('  ERROR: No GitHub PAT set - cannot fetch PRs from GitHub')
        return {}, {}, {}
    print('  GitHub PAT: Available')
    sys.stdout.flush()
    time.sleep(0.1)

    # 2. Connect to DB to get miner identities and "before" scores
    print('\n[2/8] Loading miner identities and existing scores from DB...')
    sys.stdout.flush()
    time.sleep(0.1)
    conn = create_db_connection()
    if not conn:
        return {}, {}, {}
    miners = load_miner_identities(conn)

    # Load "before" data for comparison
    before_pr_scores = load_before_pr_scores(conn, miners)
    before_miner_scores = {uid: float(total_score) if total_score else 0.0 for uid, _, _, total_score in miners}
    print(f'  Loaded {len(before_pr_scores)} PR scores from DB for comparison')
    conn.close()

    # 3. Create evaluations and fetch PRs from GitHub
    print('\n[3/8] Fetching PRs from GitHub for each miner...')
    sys.stdout.flush()
    time.sleep(0.1)
    evals: Dict[int, MinerEvaluation] = {}
    for uid, hotkey, github_id, _ in miners:
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
        print('\n[4/9] No custom evaluations.')
    sys.stdout.flush()
    time.sleep(0.1)

    # 5. Score PRs (uses token-based scoring with file contents from GitHub)
    print('\n[5/8] Scoring PRs with token-based scoring...')
    sys.stdout.flush()
    time.sleep(0.1)
    for uid, ev in evals.items():
        if ev.failed_reason:
            continue
        score_miner_prs(ev, master_repos, prog_langs, token_config)

    # 6. Detect duplicate GitHub accounts
    print('\n[6/9] Checking for duplicate GitHub accounts...')
    sys.stdout.flush()
    time.sleep(0.1)
    detect_and_penalize_miners_sharing_github(evals)

    # 7. Finalize scores
    print('\n[7/9] Finalizing scores...')
    sys.stdout.flush()
    time.sleep(0.1)
    finalize_miner_scores(evals)

    # 8. Normalize & apply dynamic emissions
    print('\n[8/9] Normalizing & applying dynamic emissions...')
    sys.stdout.flush()
    time.sleep(0.1)
    normalized = normalize_rewards_linear(evals)
    scaled = apply_dynamic_emissions_using_network_contributions(normalized, evals)

    # 9. Store evaluations (optional)
    if store_evaluations:
        print('\n[9/9] Storing evaluations to database...')
        sys.stdout.flush()
        time.sleep(0.1)
        db_storage = DatabaseStorage()
        if db_storage.is_enabled():
            for uid, miner_eval in evals.items():
                result = db_storage.store_evaluation(miner_eval)
                if result.success:
                    print(f'  Stored UID {uid}')
                else:
                    print(f'  Failed UID {uid}: {result.errors}')
            db_storage.close()
        else:
            print('  WARNING: Database storage not enabled. Check DB env vars.')
    else:
        print('\n[9/9] Skipping evaluation storage (store_evaluations=False)')

    # Build comparison data
    pr_comparisons = build_pr_comparisons(evals, before_pr_scores)
    miner_comparisons = build_miner_comparisons(evals, before_miner_scores)

    # Let logs flush before printing the clean report
    print('\nCalculating analysis...')
    sys.stdout.flush()
    time.sleep(3)

    # Print analysis report (replaces basic summary)
    print_analysis_report(evals, pr_comparisons, miner_comparisons, normalized, scaled)

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
    import argparse

    parser = argparse.ArgumentParser(description='Run scoring simulation')
    parser.add_argument('--store', action='store_true', help='Store evaluations to database')
    parser.add_argument('--no-custom', action='store_true', help='Exclude custom evaluations')
    args = parser.parse_args()

    try:
        run_scoring_simulation(
            include_custom=not args.no_custom,
            store_evaluations=args.store,
        )
    except KeyboardInterrupt:
        print('\nInterrupted')
        sys.exit(0)
    except Exception as e:
        print(f'ERROR: {e}')
        import traceback

        traceback.print_exc()
        sys.exit(1)
