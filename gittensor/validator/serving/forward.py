# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving verification loop (sub-subnet B beta).

Sends golden-output inference challenges to every serving axon and produces a
per-UID serving score for the serving emission pool. With the deterministic
echo backend the validator derives the exact expected completion locally, so
correctness is a string match and latency comes from dendrite process time —
the same verification shape (audit prompts + latency bands) the real runtime
graduates into.
"""

import secrets
from typing import TYPE_CHECKING, Dict, List, Tuple

import bittensor as bt

from gittensor.constants import (
    SERVING_CHALLENGE_TIMEOUT,
    SERVING_CHALLENGES_PER_ROUND,
)
from gittensor.serving.backends import expected_completion
from gittensor.serving.loadout import ServingLoadout, load_serving_loadout
from gittensor.synapses import InferenceSynapse
from gittensor.validator.serving.scoring import challenge_score

if TYPE_CHECKING:
    from neurons.validator import Validator


async def serving_challenges(self: 'Validator', miner_uids: set[int]) -> Dict[int, float]:
    """Challenge each serving axon and return UID -> serving score in [0, 1]."""
    loadout = load_serving_loadout()
    serving = get_serving_axons(self, miner_uids)
    if not serving:
        bt.logging.info('Serving: no serving axons found this round')
        return {}

    uids = [uid for uid, _ in serving]
    axons = [axon for _, axon in serving]
    totals: Dict[int, float] = {uid: 0.0 for uid in uids}

    for _ in range(SERVING_CHALLENGES_PER_ROUND):
        prompt = secrets.token_hex(16)
        expected = expected_completion(prompt, loadout.max_tokens, loadout.model_id)
        synapse = InferenceSynapse(prompt=prompt, model_id=loadout.model_id, max_tokens=loadout.max_tokens)

        responses = await self.dendrite(
            axons=axons,
            synapse=synapse,
            deserialize=False,
            timeout=SERVING_CHALLENGE_TIMEOUT,
        )

        for uid, response in zip(uids, responses):
            totals[uid] += _score_response(response, expected, loadout)

    scores = {uid: total / SERVING_CHALLENGES_PER_ROUND for uid, total in totals.items()}
    for uid, score in sorted(scores.items()):
        bt.logging.info(f'Serving: UID {uid} score {score:.3f} over {SERVING_CHALLENGES_PER_ROUND} challenges')
    return scores


def get_serving_axons(self: 'Validator', miner_uids: set[int]) -> List[Tuple[int, bt.AxonInfo]]:
    """UIDs (excluding self) whose axon is serving — the candidate serving miners.

    Beta heuristic: axon.is_serving is the only signal. Validators also serve
    axons (for PAT handling), so they will appear here and simply score zero on
    inference challenges; a serving-miner registry replaces this later.
    """
    serving: List[Tuple[int, bt.AxonInfo]] = []
    for uid in sorted(miner_uids):
        if uid == self.uid:
            continue
        axon = self.metagraph.axons[uid]
        if axon is not None and axon.is_serving:
            serving.append((uid, axon))
    return serving


def _score_response(response: InferenceSynapse, expected: str, loadout: ServingLoadout) -> float:
    completion = getattr(response, 'completion', None)
    if completion is None:
        return 0.0
    served_model = getattr(response, 'served_model_id', None)
    if served_model != loadout.model_id:
        return 0.0
    process_time = getattr(response.dendrite, 'process_time', None)
    elapsed_ms = float(process_time) * 1000.0 if process_time is not None else float('inf')
    return challenge_score(completion == expected, elapsed_ms)
