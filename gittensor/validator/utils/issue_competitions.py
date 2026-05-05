# The MIT License (MIT)
# Copyright 2025 Entrius

"""Utility functions for Issue Bounties sub-mechanism."""

from typing import Optional

import bittensor as bt


def get_miner_coldkey(hotkey: str, subtensor: bt.Subtensor) -> Optional[str]:
    """
    Get the coldkey for a miner's hotkey.

    Args:
        hotkey: Miner's hotkey address
        subtensor: Bittensor subtensor instance

    Returns:
        Coldkey address or None
    """
    try:
        result = subtensor.get_hotkey_owner(hotkey)
        if result:
            return str(result)
    except Exception as e:
        bt.logging.debug(f'Error getting coldkey for {hotkey}: {e}')
    return None
