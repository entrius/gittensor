# The MIT License (MIT)
# Copyright 2025 Entrius

"""Utility functions for Issue Bounties sub-mechanism."""

import json
import os
from pathlib import Path
from typing import Dict, Optional

import bittensor as bt

from gittensor.constants import CONTRACT_ADDRESS

_CONFIG_FILE = Path.home() / '.gittensor' / 'config.json'


def get_contract_address() -> Optional[str]:
    """
    Get contract address. env var > ~/.gittensor/config.json > constants.py default.

    Returns:
        Contract address string
    """
    env_val = os.environ.get('CONTRACT_ADDRESS')
    if env_val:
        return env_val
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE, 'r') as f:
                config = json.load(f)
            if config.get('contract_address'):
                return config['contract_address']
        except (json.JSONDecodeError, IOError):
            pass
    return CONTRACT_ADDRESS


def get_miner_coldkey(hotkey: str, subtensor: bt.Subtensor, netuid: int) -> Optional[str]:
    """
    Get the coldkey for a miner's hotkey.

    Args:
        hotkey: Miner's hotkey address
        subtensor: Bittensor subtensor instance
        netuid: Network UID

    Returns:
        Coldkey address or None
    """
    try:
        result = subtensor.query_subtensor("Owner", None, [hotkey])
        if result:
            return str(result)
    except Exception as e:
        bt.logging.debug(f"Error getting coldkey for {hotkey}: {e}")
    return None
