# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Transport protocol between the consensus core and its data sources.

Baskets/registry come from the repos-v0 contract; stakes/permits stay a
runtime (metagraph) concern but sit behind the same protocol for symmetry.
Snapshot reads MUST be pinned to the snapshot block's hash so every validator
aggregates over byte-identical state even across reorgs.
"""

from typing import Dict, Optional, Protocol, Tuple


class ConsensusBackend(Protocol):
    def fetch_registry(self, snapshot: int) -> Dict[int, str]:
        """Active repos at the snapshot block: github_id -> full_name."""
        ...

    def fetch_baskets(self, snapshot: int) -> Dict[str, Dict[str, int]]:
        """All whitelisted voters' baskets at the snapshot block:
        hotkey_ss58 -> {full_name: weight}."""
        ...

    def fetch_own_basket(self) -> Optional[Dict[str, int]]:
        """The validator's currently published basket, name-keyed, if any."""
        ...

    def publish_basket(self, prefs: Dict[str, int]) -> bool:
        """Publish the canonical preference vector; True when on-chain state
        matches the desired prefs on exit."""
        ...

    def fetch_stakes_and_permits(self, snapshot: int) -> Tuple[Dict[str, int], Dict[str, bool]]:
        """Metagraph state at the snapshot block: (hotkey -> stake rao,
        hotkey -> validator permit)."""
        ...
