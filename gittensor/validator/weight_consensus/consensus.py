# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Deterministic aggregation of validator repo weight preferences.

Every validator computes this over the identical chain snapshot, so the math
runs entirely in python big-ints — floats appear only in the final division of
identical integers, guaranteeing byte-identical shares on every machine.
"""

from dataclasses import dataclass, replace
from typing import Dict, Mapping, Optional

from gittensor.validator.utils.config import (
    CONSENSUS_MIN_VALIDATOR_STAKE_RAO,
    CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS,
    CONSENSUS_WEIGHT_PRECISION,
)
from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.codec import decode_prefs


@dataclass(frozen=True)
class AggregateResult:
    """Outcome of one snapshot aggregation.

    ``shares`` is None when the activation gate failed — callers fall back to
    the baked-in repository weights. ``numerators`` are the exact integers the
    disk cache persists so a reload reproduces identical float shares.
    """

    shares: Optional[Dict[str, float]]
    numerators: Dict[str, int]
    eligible_stake_rao: int
    valid_stake_rao: int
    voter_count: int


def compute_snapshot_block(block: int) -> int:
    return block - (block % CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS)


def aggregate_preferences(
    commitments: Mapping[str, bytes],
    stakes_rao: Mapping[str, int],
    validator_permits: Mapping[str, bool],
) -> AggregateResult:
    """Stake-weighted mean of eligible validators' preference vectors.

    Eligible voters hold a validator permit and >= the stake threshold at the
    snapshot block. Each valid vector is normalized to sum 1.0 in fixed-point
    (``S_rao * w * PREC // W_v``) before the stake-weighted accumulation, so a
    voter's influence is exactly proportional to stake. The activation gate
    passes when valid voters hold at least half the eligible stake.
    """
    eligible_stake = 0
    valid_stake = 0
    voter_count = 0
    numerators: Dict[str, int] = {}

    for hotkey in sorted(stakes_rao):
        stake = stakes_rao[hotkey]
        if not validator_permits.get(hotkey) or stake < CONSENSUS_MIN_VALIDATOR_STAKE_RAO:
            continue
        eligible_stake += stake

        payload = commitments.get(hotkey)
        prefs = decode_prefs(payload) if payload else None
        if prefs is None:
            continue

        valid_stake += stake
        voter_count += 1
        basket_total = sum(prefs.values())
        for repo, weight in prefs.items():
            numerators[repo] = numerators.get(repo, 0) + stake * weight * CONSENSUS_WEIGHT_PRECISION // basket_total

    gate_passed = eligible_stake > 0 and valid_stake * 2 >= eligible_stake and numerators
    return AggregateResult(
        shares=shares_from_numerators(numerators) if gate_passed else None,
        numerators=numerators,
        eligible_stake_rao=eligible_stake,
        valid_stake_rao=valid_stake,
        voter_count=voter_count,
    )


def shares_from_numerators(numerators: Mapping[str, int]) -> Dict[str, float]:
    """Convert integer numerators to float shares summing ~1.0. Deterministic:
    identical ints divide to identical doubles on every platform."""
    total = sum(numerators.values())
    return {repo: numerators[repo] / total for repo in sorted(numerators)}


def apply_consensus(
    master: Dict[str, RepositoryConfig],
    shares: Optional[Mapping[str, float]],
) -> Dict[str, RepositoryConfig]:
    """Overlay consensus shares onto the baked repository registry.

    None shares (gate failed / no aggregate) leaves the registry untouched.
    Otherwise the aggregate is the complete share vector: registry repos keep
    their tuned config with the share overridden (0.0 when unvoted — the
    aggregate sums to 1.0, keeping baked shares would double-count), and novel
    repos enter with default config.
    """
    if shares is None:
        return master

    return {
        name: replace(master[name], emission_share=shares.get(name, 0.0))
        if name in master
        else RepositoryConfig(emission_share=shares[name])
        for name in sorted(set(master) | set(shares))
    }
