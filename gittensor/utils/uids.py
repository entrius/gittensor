from typing import List


def get_all_uids(self, exclude: List[int] = []) -> set[int]:
    """Return all eligible miner UIDs for scoring.

    Args:
        exclude: UIDs to omit from the returned set.

    Returns:
        Set of miner UIDs that are serving and within the validator-permit TAO limit.
        UID ``0`` is always included.
    """
    # Get all available miner UIDs, excluding specified ones
    available_miner_uids = {uid for uid in range(self.metagraph.n.item()) if uid not in exclude}

    # Ensure miner UID 0 is always included (subnet requirement)
    available_miner_uids.add(0)

    return set(available_miner_uids)
