from typing import List

import bittensor as bt
import numpy as np


def check_uid_availability(metagraph: "bt.metagraph.Metagraph", uid: int, vpermit_tao_limit: int) -> bool:
    """Check if uid is available. The UID should be available if it is serving and has less than vpermit_tao_limit stake
    Args:
        metagraph (:obj: bt.metagraph.Metagraph): Metagraph object
        uid (int): uid to be checked
        vpermit_tao_limit (int): Validator permit tao limit
    Returns:
        bool: True if uid is available, False otherwise
    """
    # Filter non serving axons.
    if not metagraph.axons[uid].is_serving:
        return False
    # Filter validator permit > 1024 stake.
    if metagraph.validator_permit[uid]:
        if metagraph.S[uid] > vpermit_tao_limit:
            return False
    # Available otherwise.
    return True


def get_all_uids(self, exclude: List[int] = []) -> set[int]:
    """Returns all uids from the metagraph, excluding specified ones.
    Args:
        exclude (List[int]): List of uids to exclude from the result.
    Returns:
        uids (set[int]): All uids excluding specified ones. UID 0 is always included at the beginning.
    """
    # Get all available miner UIDs, excluding specified ones
    available_miner_uids = {uid for uid in range(self.metagraph.n.item()) if uid not in exclude}

    # Ensure miner UID 0 is always included (subnet requirement)
    available_miner_uids.add(0)

    return available_miner_uids
