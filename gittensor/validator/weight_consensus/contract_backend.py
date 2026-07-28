# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Contract-backed transport for the weight consensus.

Snapshot reads resolve the snapshot block to its HASH once and pin every
childstate read to it, so all validators aggregate over byte-identical state
even across reorgs. Publishing signs ``set_basket`` with the validator hotkey;
repo names resolve to github ids through the registry at a single head hash.
"""

from typing import Dict, List, Optional, Tuple

import bittensor as bt

from gittensor.validator.repo_registry.contract_client import RepoRegistryContractClient
from gittensor.validator.weight_consensus.codec import canonicalize_prefs


class ContractBackend:
    """ConsensusBackend over the repos-v0 registry contract."""

    def __init__(self, client: RepoRegistryContractClient, subtensor: 'bt.Subtensor', wallet: 'bt.Wallet', netuid: int):
        self.client = client
        self.subtensor = subtensor
        self.wallet = wallet
        self.netuid = netuid

    def _snapshot_hash(self, snapshot: int) -> str:
        block_hash = self.subtensor.substrate.get_block_hash(snapshot)
        if not block_hash:
            raise RuntimeError(f'No block hash for snapshot block {snapshot}')
        return block_hash

    def _registry_at(self, at: str) -> Dict[int, str]:
        return {repo.github_id: repo.full_name for repo in self.client.get_all_repos(at=at)}

    def _resolve_names(self, entries: List[Tuple[int, int]], names: Dict[int, str]) -> Dict[str, int]:
        """Map basket entries to name-keyed prefs, dropping deregistered ids.
        Weights accumulate should two ids ever share a name."""
        prefs: Dict[str, int] = {}
        for github_id, weight in entries:
            name = names.get(github_id)
            if name is not None:
                prefs[name] = prefs.get(name, 0) + weight
        return prefs

    def fetch_registry(self, snapshot: int) -> Dict[int, str]:
        return self._registry_at(self._snapshot_hash(snapshot))

    def fetch_baskets(self, snapshot: int) -> Dict[str, Dict[str, int]]:
        at = self._snapshot_hash(snapshot)
        names = self._registry_at(at)
        baskets = {}
        for hotkey, entries in self.client.get_all_baskets(at=at).items():
            prefs = self._resolve_names(entries, names)
            if prefs:
                baskets[hotkey] = prefs
        return baskets

    def fetch_own_basket(self) -> Optional[Dict[str, int]]:
        at = self.subtensor.substrate.get_chain_head()
        entries = self.client.get_basket(self.wallet.hotkey.ss58_address, at=at)
        if not entries:
            return None
        return self._resolve_names(entries, self._registry_at(at)) or None

    def publish_basket(self, prefs: Dict[str, int]) -> bool:
        at = self.subtensor.substrate.get_chain_head()
        ids = {name: github_id for github_id, name in sorted(self._registry_at(at).items())}

        unknown = sorted(set(prefs) - set(ids))
        if unknown:
            bt.logging.warning(f'weight_consensus: dropping unregistered repos from basket: {", ".join(unknown)}')
            prefs = {name: weight for name, weight in prefs.items() if name in ids}
            if not prefs:
                return False
            prefs = canonicalize_prefs(prefs)

        entries = sorted((ids[name], weight) for name, weight in prefs.items())
        current = self.client.get_basket(self.wallet.hotkey.ss58_address, at=at)
        if current is not None and sorted(current) == entries:
            return True
        return self.client.set_basket(entries, self.wallet)

    def fetch_stakes_and_permits(self, snapshot: int) -> Tuple[Dict[str, int], Dict[str, bool]]:
        metagraph = self.subtensor.metagraph(self.netuid, block=snapshot, lite=True)
        stakes_rao = {hk: int(round(float(metagraph.S[uid]) * 1e9)) for uid, hk in enumerate(metagraph.hotkeys)}
        permits = {hk: bool(metagraph.validator_permit[uid]) for uid, hk in enumerate(metagraph.hotkeys)}
        return stakes_rao, permits
