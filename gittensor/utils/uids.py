def get_all_uids(self) -> set[int]:
    """Return all miner UIDs for scoring.

    Returns:
        Set of miner UIDs in the metagraph. UID ``0`` is always included
        (subnet requirement) so the empty-metagraph bootstrap case still
        yields ``{0}`` rather than ``set()``.
    """
    available_miner_uids = set(range(self.metagraph.n.item()))
    available_miner_uids.add(0)
    return available_miner_uids
