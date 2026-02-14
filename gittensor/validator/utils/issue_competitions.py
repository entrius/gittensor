# The MIT License (MIT)
# Copyright 2025 Entrius

"""Utility functions for Issue Bounties sub-mechanism."""

import os
from typing import Dict, Optional

import bittensor as bt

from gittensor.constants import CONTRACT_ADDRESS


def get_contract_address() -> Optional[str]:
    """
    Get contract address. Override via CONTRACT_ADDRESS env var for dev/testing.

    Returns:
        Contract address string (env var override or constants.py default)
    """
    return os.environ.get('CONTRACT_ADDRESS') or CONTRACT_ADDRESS


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
        result = subtensor.get_hotkey_owner(hotkey)
        if result:
            return str(result)
    except Exception as e:
        bt.logging.debug(f"Error getting coldkey for {hotkey}: {e}")
    return None
