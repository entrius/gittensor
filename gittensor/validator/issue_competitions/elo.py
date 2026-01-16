# The MIT License (MIT)
# Copyright 2025 Entrius

"""ELO rating system with rolling 30-day exponential moving average."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import bittensor as bt

from .constants import (
    ELO_CUTOFF,
    EMA_DECAY_FACTOR,
    INITIAL_ELO,
    K_FACTOR,
    LOOKBACK_DAYS,
)


@dataclass
class CompetitionRecord:
    """Record of a completed competition for ELO calculation."""

    competition_id: int
    completed_at: datetime
    is_winner: bool
    opponent_elo: int
    bounty_amount: int


@dataclass
class EloRating:
    """ELO rating for a miner."""

    uid: int
    hotkey: str
    elo: int = INITIAL_ELO
    wins: int = 0
    losses: int = 0
    last_competition_at: Optional[datetime] = None
    is_eligible: bool = True

    def __post_init__(self):
        """Set eligibility based on ELO."""
        self.is_eligible = is_eligible(self.elo)


def calculate_expected_score(player_elo: int, opponent_elo: int) -> float:
    """
    Calculate expected score using ELO formula.

    Args:
        player_elo: Player's current ELO rating
        opponent_elo: Opponent's ELO rating

    Returns:
        Expected score between 0 and 1
    """
    return 1.0 / (1.0 + 10 ** ((opponent_elo - player_elo) / 400.0))


def calculate_elo_change(
    current_elo: int,
    opponent_elo: int,
    is_winner: bool,
    k_factor: int = K_FACTOR,
) -> int:
    """
    Calculate ELO change for a single match.

    Args:
        current_elo: Player's current ELO
        opponent_elo: Opponent's ELO
        is_winner: True if player won
        k_factor: K-factor for volatility

    Returns:
        Change in ELO (positive for win, negative for loss)
    """
    expected = calculate_expected_score(current_elo, opponent_elo)
    actual = 1.0 if is_winner else 0.0
    return round(k_factor * (actual - expected))


def calculate_elo_ema(
    competitions: List[CompetitionRecord],
    now: Optional[datetime] = None,
) -> int:
    """
    Calculate ELO using exponential moving average over last 30 days.

    Recent competitions are weighted more heavily using the EMA decay factor.
    After 30 days of inactivity, ELO returns toward the initial rating (~800).

    Algorithm:
    1. Filter competitions to last LOOKBACK_DAYS
    2. Sort by date ascending (oldest first)
    3. Apply time-weighted EMA: weight = EMA_DECAY_FACTOR ^ days_ago
    4. Sum weighted ELO changes and apply to initial ELO

    Args:
        competitions: List of competition records for this miner
        now: Current timestamp (defaults to UTC now)

    Returns:
        Calculated ELO rating
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Calculate lookback cutoff
    lookback_cutoff = now - timedelta(days=LOOKBACK_DAYS)

    # Filter to recent competitions
    recent_competitions = [
        c for c in competitions
        if c.completed_at >= lookback_cutoff
    ]

    # If no recent competitions, return initial ELO (rating decay)
    if not recent_competitions:
        bt.logging.debug('No recent competitions, returning initial ELO')
        return INITIAL_ELO

    # Sort by date ascending (oldest first for sequential processing)
    recent_competitions.sort(key=lambda c: c.completed_at)

    # Calculate ELO changes with time-weighted EMA
    current_elo = INITIAL_ELO
    weighted_changes = []

    for comp in recent_competitions:
        # Calculate days ago for weighting
        days_ago = (now - comp.completed_at).total_seconds() / (24 * 3600)
        weight = EMA_DECAY_FACTOR ** days_ago

        # Calculate ELO change for this match
        elo_change = calculate_elo_change(
            current_elo,
            comp.opponent_elo,
            comp.is_winner,
        )

        # Apply weighted change
        weighted_change = elo_change * weight
        weighted_changes.append(weighted_change)

        # Update running ELO for next calculation
        current_elo = max(1, current_elo + elo_change)

    # Apply total weighted changes to initial ELO
    total_weighted_change = sum(weighted_changes)
    final_elo = max(1, INITIAL_ELO + round(total_weighted_change))

    bt.logging.debug(
        f'ELO calculation: {len(recent_competitions)} competitions, '
        f'weighted change={total_weighted_change:.2f}, final ELO={final_elo}'
    )

    return final_elo


def is_eligible(elo: int) -> bool:
    """
    Check if miner is eligible to compete based on ELO.

    Miners below ELO_CUTOFF are ineligible for new competitions.

    Args:
        elo: Miner's current ELO rating

    Returns:
        True if eligible (ELO >= ELO_CUTOFF)
    """
    return elo >= ELO_CUTOFF


def get_elo_rankings(
    miner_competitions: Dict[str, List[CompetitionRecord]],
    miner_info: Dict[str, Dict],  # hotkey -> {uid: int}
    now: Optional[datetime] = None,
) -> List[EloRating]:
    """
    Calculate ELO ratings for all miners, sorted descending by ELO.

    Args:
        miner_competitions: Dict mapping hotkey -> list of CompetitionRecords
        miner_info: Dict mapping hotkey -> {uid: int}
        now: Current timestamp (defaults to UTC now)

    Returns:
        List of EloRating objects sorted by ELO descending
    """
    if now is None:
        now = datetime.now(timezone.utc)

    elo_ratings = []

    for hotkey, competitions in miner_competitions.items():
        info = miner_info.get(hotkey, {})
        uid = info.get('uid', 0)

        # Calculate ELO using EMA
        elo = calculate_elo_ema(competitions, now)

        # Count wins/losses
        wins = sum(1 for c in competitions if c.is_winner)
        losses = sum(1 for c in competitions if not c.is_winner)

        # Get last competition date
        last_competition_at = None
        if competitions:
            last_competition_at = max(c.completed_at for c in competitions)

        rating = EloRating(
            uid=uid,
            hotkey=hotkey,
            elo=elo,
            wins=wins,
            losses=losses,
            last_competition_at=last_competition_at,
            is_eligible=is_eligible(elo),
        )
        elo_ratings.append(rating)

    # Sort by ELO descending
    elo_ratings.sort(key=lambda r: r.elo, reverse=True)

    bt.logging.info(f'Calculated ELO for {len(elo_ratings)} miners')
    if elo_ratings:
        top_3 = elo_ratings[:3]
        bt.logging.info(
            f'Top 3 ELO: {[(r.hotkey[:8], r.elo) for r in top_3]}'
        )

    return elo_ratings


def get_elo_for_hotkey(
    hotkey: str,
    miner_competitions: Dict[str, List[CompetitionRecord]],
    now: Optional[datetime] = None,
) -> int:
    """
    Get ELO rating for a specific hotkey.

    Args:
        hotkey: Miner's hotkey
        miner_competitions: Dict mapping hotkey -> list of CompetitionRecords
        now: Current timestamp

    Returns:
        ELO rating (INITIAL_ELO if no competition history)
    """
    competitions = miner_competitions.get(hotkey, [])
    return calculate_elo_ema(competitions, now)
