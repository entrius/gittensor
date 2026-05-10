# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Typing-only boundary between ``gittensor.validator`` and neuron runtime."""

from __future__ import annotations

from typing import Dict, List, Protocol, Set

import numpy as np

from gittensor.classes import MinerEvaluation


class ValidatorWorkflowProtocol(Protocol):
    """Methods and attributes the scoring loop expects from the neuron ``Validator``.

    Keeps ``forward`` and OSS reward code from importing ``neurons.validator`` for type hints.
    """

    step: int
    metagraph: object

    async def bulk_store_evaluation(
        self,
        miner_evals: Dict[int, MinerEvaluation],
        skip_uids: Set[int] | None = None,
    ) -> None: ...

    def update_scores(
        self,
        rewards: np.ndarray,
        uids: set[int],
        blacklisted_uids: List[int] | None = None,
    ) -> None: ...

    def store_or_use_cached_evaluation(self, miner_evaluations: Dict[int, MinerEvaluation]) -> Set[int]: ...
