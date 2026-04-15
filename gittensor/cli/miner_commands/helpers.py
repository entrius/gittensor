# Entrius 2025

"""Shared helper utilities for miner CLI commands."""

from __future__ import annotations


def _get_validator_axons(metagraph) -> tuple[list, list]:
    """Return (axons, uids) for all active validators (vtrust > 0.1, serving)."""
    axons = []
    uids = []
    for uid in range(metagraph.n):
        if metagraph.validator_trust[uid] > 0.1 and metagraph.axons[uid].is_serving:
            axons.append(metagraph.axons[uid])
            uids.append(uid)
    return axons, uids
