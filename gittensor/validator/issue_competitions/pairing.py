# The MIT License (MIT)
# Copyright 2025 Entrius

"""ELO-priority pairing algorithm for issue competitions."""

from typing import Dict, List, Optional, Set, Tuple

import bittensor as bt

from .contract_client import ContractIssue
from .elo import EloRating


def find_pairs_for_issues(
    available_issues: List[ContractIssue],
    miner_preferences: Dict[str, List[int]],
    elo_ratings: Dict[str, EloRating],
    miners_in_competition: Set[str],
) -> List[Tuple[int, str, str]]:
    """
    Find miner pairs for available issues using ELO-priority matching.

    Algorithm:
    1. For each issue, collect miners who expressed preference for it
    2. Filter out miners already in competition or below ELO cutoff
    3. Sort eligible miners by ELO descending
    4. Take top 2 as the pair (highest ELO miners get priority)

    Args:
        available_issues: List of issues available for competition (Active status)
        miner_preferences: Dict mapping hotkey -> ranked list of issue IDs (most preferred first)
        elo_ratings: Dict mapping hotkey -> EloRating
        miners_in_competition: Set of hotkeys already in active competitions

    Returns:
        List of tuples (issue_id, miner1_hotkey, miner2_hotkey)
        where miner1 has higher or equal ELO than miner2
    """
    pairs: List[Tuple[int, str, str]] = []

    # Track miners we've already paired in this round
    paired_miners: Set[str] = set()

    bt.logging.info(
        f'Finding pairs for {len(available_issues)} issues '
        f'from {len(miner_preferences)} miners with preferences'
    )

    for issue in available_issues:
        issue_id = issue.id

        # Collect miners who want this issue
        interested_miners = get_interested_miners(
            issue_id,
            miner_preferences,
            elo_ratings,
            miners_in_competition,
            paired_miners,
        )

        if len(interested_miners) < 2:
            bt.logging.debug(
                f'Issue {issue_id}: Only {len(interested_miners)} eligible miners, need 2'
            )
            continue

        # Sort by ELO descending
        interested_miners.sort(key=lambda h: elo_ratings.get(h, EloRating(0, h)).elo, reverse=True)

        # Take top 2
        miner1 = interested_miners[0]
        miner2 = interested_miners[1]

        pairs.append((issue_id, miner1, miner2))

        # Mark as paired for this round
        paired_miners.add(miner1)
        paired_miners.add(miner2)

        bt.logging.info(
            f'Paired for issue {issue_id}: '
            f'{miner1[:8]}... (ELO={elo_ratings.get(miner1, EloRating(0, miner1)).elo}) vs '
            f'{miner2[:8]}... (ELO={elo_ratings.get(miner2, EloRating(0, miner2)).elo})'
        )

    bt.logging.info(f'Found {len(pairs)} pairs for issues')
    return pairs


def get_interested_miners(
    issue_id: int,
    miner_preferences: Dict[str, List[int]],
    elo_ratings: Dict[str, EloRating],
    miners_in_competition: Set[str],
    paired_miners: Set[str],
) -> List[str]:
    """
    Get eligible miners interested in a specific issue.

    Filters for:
    - Miners who listed this issue in their preferences
    - Not already in an active competition
    - Not already paired in this round
    - ELO above cutoff (is_eligible = True)

    Args:
        issue_id: The issue to find interested miners for
        miner_preferences: Dict mapping hotkey -> ranked list of issue IDs
        elo_ratings: Dict mapping hotkey -> EloRating
        miners_in_competition: Set of hotkeys already in active competitions
        paired_miners: Set of hotkeys already paired in this round

    Returns:
        List of eligible miner hotkeys
    """
    interested: List[str] = []

    for hotkey, preferences in miner_preferences.items():
        # Check if miner expressed interest in this issue
        if issue_id not in preferences:
            continue

        # Check if miner is already in a competition
        if hotkey in miners_in_competition:
            bt.logging.debug(f'{hotkey[:8]}... already in competition')
            continue

        # Check if miner is already paired this round
        if hotkey in paired_miners:
            bt.logging.debug(f'{hotkey[:8]}... already paired this round')
            continue

        # Check ELO eligibility
        rating = elo_ratings.get(hotkey)
        if rating is None:
            # New miner with no rating - use default (eligible by default)
            interested.append(hotkey)
        elif rating.is_eligible:
            interested.append(hotkey)
        else:
            bt.logging.debug(
                f'{hotkey[:8]}... ELO {rating.elo} below cutoff, ineligible'
            )

    return interested


def get_preference_rank(hotkey: str, issue_id: int, miner_preferences: Dict[str, List[int]]) -> int:
    """
    Get the preference rank for a miner-issue pair.

    Lower rank = higher preference (0 = most preferred).

    Args:
        hotkey: Miner's hotkey
        issue_id: Issue ID
        miner_preferences: Dict mapping hotkey -> ranked list of issue IDs

    Returns:
        Preference rank (index in list), or 999 if not in preferences
    """
    preferences = miner_preferences.get(hotkey, [])
    try:
        return preferences.index(issue_id)
    except ValueError:
        return 999


def find_best_pair_for_issue(
    issue_id: int,
    miner_preferences: Dict[str, List[int]],
    elo_ratings: Dict[str, EloRating],
    miners_in_competition: Set[str],
) -> Optional[Tuple[str, str]]:
    """
    Find the best pair for a single issue.

    Utility function for finding pairs one at a time.

    Args:
        issue_id: The issue to pair miners for
        miner_preferences: Dict mapping hotkey -> ranked list of issue IDs
        elo_ratings: Dict mapping hotkey -> EloRating
        miners_in_competition: Set of hotkeys already in active competitions

    Returns:
        Tuple of (miner1_hotkey, miner2_hotkey) or None if not enough miners
    """
    interested_miners = get_interested_miners(
        issue_id,
        miner_preferences,
        elo_ratings,
        miners_in_competition,
        paired_miners=set(),  # No paired miners for single-issue lookup
    )

    if len(interested_miners) < 2:
        return None

    # Sort by ELO descending
    interested_miners.sort(key=lambda h: elo_ratings.get(h, EloRating(0, h)).elo, reverse=True)

    return (interested_miners[0], interested_miners[1])


def calculate_pairing_stats(
    pairs: List[Tuple[int, str, str]],
    elo_ratings: Dict[str, EloRating],
) -> Dict:
    """
    Calculate statistics about the pairings.

    Args:
        pairs: List of (issue_id, miner1_hotkey, miner2_hotkey)
        elo_ratings: Dict mapping hotkey -> EloRating

    Returns:
        Dict with pairing statistics
    """
    if not pairs:
        return {
            'total_pairs': 0,
            'avg_elo_diff': 0.0,
            'max_elo_diff': 0,
            'min_elo_diff': 0,
        }

    elo_diffs = []
    for issue_id, miner1, miner2 in pairs:
        elo1 = elo_ratings.get(miner1, EloRating(0, miner1)).elo
        elo2 = elo_ratings.get(miner2, EloRating(0, miner2)).elo
        elo_diffs.append(abs(elo1 - elo2))

    return {
        'total_pairs': len(pairs),
        'avg_elo_diff': sum(elo_diffs) / len(elo_diffs),
        'max_elo_diff': max(elo_diffs),
        'min_elo_diff': min(elo_diffs),
    }
