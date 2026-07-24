# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Publishes the validator's own repo weight preferences to the chain."""

import json
from pathlib import Path
from typing import Dict, Optional

import bittensor as bt

from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.chain import fetch_own_prefs, publish_payload
from gittensor.validator.weight_consensus.codec import CodecError, canonicalize_prefs, encode_prefs


def resolve_local_prefs(prefs_path: Optional[Path], master: Dict[str, RepositoryConfig]) -> Dict[str, int]:
    """The validator's desired vote in canonical form.

    Reads ``{"version": 1, "repos": {"owner/repo": weight}}`` from prefs_path;
    a missing or invalid file falls back to voting the baked-in registry shares.
    """
    if prefs_path is not None and prefs_path.exists():
        try:
            data = json.loads(prefs_path.read_text())
            return canonicalize_prefs(data['repos'])
        except (OSError, ValueError, KeyError, TypeError, CodecError) as e:
            bt.logging.warning(f'weight_consensus: invalid prefs file {prefs_path} ({e}); voting baked-in shares')

    return canonicalize_prefs({name: cfg.emission_share for name, cfg in master.items() if cfg.emission_share > 0})


def maybe_publish_prefs(subtensor: 'bt.Subtensor', wallet: 'bt.Wallet', netuid: int, prefs: Dict[str, int]) -> bool:
    """Publish prefs unless the identical vector is already on chain.

    Compares decoded vectors, never compressed bytes (zlib output can differ
    across builds). Returns True when the on-chain state matches the desired
    prefs on exit; failures warn and retry next round.
    """
    try:
        if fetch_own_prefs(subtensor, netuid, wallet.hotkey.ss58_address) == prefs:
            return True
        if publish_payload(subtensor, wallet, netuid, encode_prefs(prefs)):
            bt.logging.info(f'weight_consensus: published preference vector ({len(prefs)} repos)')
            return True
        bt.logging.warning('weight_consensus: commitment publish rejected; will retry next round')
    except Exception as e:
        bt.logging.warning(f'weight_consensus: commitment publish failed ({e}); will retry next round')
    return False
