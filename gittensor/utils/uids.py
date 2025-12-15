from typing import List

import bittensor as bt
import numpy as np


def check_uid_availability(metagraph: "bt.metagraph.Metagraph", uid: int, vpermit_tao_limit: int) -> bool:
    """Return whether a UID is eligible for querying.

    Args:
        metagraph: Metagraph containing axon and stake state.
        uid: UID to check.
        vpermit_tao_limit: Maximum allowed stake for validator-permit UIDs.

    Returns:
        True if the UID is serving and within the validator-permit stake limit.
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
    """Return all eligible miner UIDs for scoring.

    Args:
        exclude: UIDs to omit from the returned set.

    Returns:
        Set of miner UIDs that are serving and within the validator-permit TAO limit.
        UID ``0`` is always included.
    """
    metagraph: "bt.metagraph.Metagraph" = self.metagraph
    vpermit_tao_limit: int = getattr(self.config.neuron, "vpermit_tao_limit", 4096)

    # Get all available miner UIDs, excluding specified ones and applying
    # serving / vpermit filters.
    available_miner_uids = {
        uid
        for uid in range(metagraph.n.item())
        if uid not in exclude and check_uid_availability(metagraph, uid, vpermit_tao_limit)
    }

    # Ensure miner UID 0 is always included (subnet requirement)
    available_miner_uids.add(0)

    return available_miner_uids
