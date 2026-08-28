# The MIT License (MIT)
# Copyright © 2025 Entrius

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

"""Serving miner neuron (sub-subnet B beta).

Compute-miner archetype: serve inference for one blessed release, answer
validator challenges, get scored. Run it like the validator entrypoint:

    python neurons/serving_miner.py --netuid 74 --wallet.name miner --wallet.hotkey default \\
        --subtensor.network local --axon.port 8091

The release comes from the shared serving loadout (SERVING_RELEASE picks a
release_id, default = primary; openai-compat = a local
sparkinfer_server; echo = deterministic GPU-free mock for localnet via
SERVING_LOADOUT_PATH=.../serving_loadout.echo.json).
Miners run this blessed neuron unmodified — serving reward is availability
and correctness based, so there is nothing to gain by editing it.
"""

import asyncio
import os
import time
from collections import OrderedDict
from functools import partial
from typing import Dict, Optional, Tuple

import bittensor as bt
import requests
from bittensor.core.stream import StreamingSynapse
from bittensor.utils.axon_utils import allowed_nonce_window_ns, calculate_diff_seconds
from bittensor_wallet import Keypair

from gittensor.constants import (
    BLOCKS_PER_TEMPO,
    SERVING_BACKEND_CONCURRENCY,
    SERVING_MAX_TOKENS,
    SERVING_MIN_CALLER_STAKE,
    SERVING_SEEN_NONCES,
    SERVING_VALIDATOR_TOKENS_PER_TEMPO,
)
from gittensor.serving.backends import InferenceBackend, load_backend
from gittensor.serving.loadout import load_serving_loadout
from gittensor.synapses import AttestSynapse, InferenceSynapse
from neurons.base.neuron import BaseNeuron

BTStreamingResponse = StreamingSynapse.BTStreamingResponse


class ServingMiner(BaseNeuron):
    """Serves one release over an axon and answers inference challenges."""

    neuron_type: str = 'MinerNeuron'

    def __init__(self, config=None):
        super(ServingMiner, self).__init__(config=config)

        loadout = load_serving_loadout()
        wanted = os.getenv('SERVING_RELEASE')
        self.release = loadout.get(wanted) if wanted else loadout.primary
        self.backend: InferenceBackend = load_backend(self.release)

        self.axon = bt.Axon(wallet=self.wallet, config=self.config)
        self.axon.attach(
            forward_fn=partial(handle_inference, self),
            blacklist_fn=partial(blacklist_inference, self),
            priority_fn=partial(priority_inference, self),
            verify_fn=partial(verify_inference, self),
        ).attach(
            forward_fn=partial(handle_attest, self),
            blacklist_fn=partial(blacklist_attest, self),
            priority_fn=partial(priority_attest, self),
            verify_fn=partial(verify_attest, self),
        )
        # One card's worth by default; a multi-GPU box fronting N instances sets SERVING_BACKEND_CONCURRENCY=N x 16.
        self.backend_slots = asyncio.Semaphore(
            int(os.getenv('SERVING_BACKEND_CONCURRENCY', SERVING_BACKEND_CONCURRENCY))
        )
        self.seen_nonces: 'OrderedDict[str, None]' = OrderedDict()
        self.audit_budget: Dict[str, Tuple[int, int]] = {}  # validator hotkey -> (tempo, completion tokens used)
        bt.logging.info(f'ServingMiner axon: {self.axon}')

    async def forward(self, synapse: bt.Synapse) -> bt.Synapse:
        # Satisfies the BaseNeuron ABC; axon requests route through handle_inference.
        return synapse

    def resync_metagraph(self):
        self.metagraph.sync(subtensor=self.subtensor)

    def run(self):
        self.subtensor.serve_axon(netuid=self.config.netuid, axon=self.axon)
        self.axon.start()
        bt.logging.info(
            f'ServingMiner serving | uid {self.uid} | release {self.release.release_id} | model {self.release.model_id}'
        )

        try:
            while True:
                time.sleep(60)
                self.sync()
                self.step += 1
        except KeyboardInterrupt:
            self.axon.stop()
            bt.logging.success('ServingMiner killed by keyboard interrupt.')
        except Exception as e:
            bt.logging.error(f'ServingMiner error: {e}')
            self.axon.stop()
            raise


async def handle_inference(miner: ServingMiner, synapse: InferenceSynapse) -> BTStreamingResponse:
    """Stream the backend's completion for an audit or user request through the axon.

    Backend errors end the stream without ``[DONE]``; the validator scores that as a miss.
    """
    max_tokens = max(1, min(int(synapse.max_tokens), SERVING_MAX_TOKENS))
    messages, logprobs = synapse.messages, synapse.logprobs

    async def token_streamer(send) -> None:
        loop = asyncio.get_running_loop()
        queue: 'asyncio.Queue[Optional[bytes]]' = asyncio.Queue()

        def produce() -> None:
            try:
                for chunk in miner.backend.stream(messages, max_tokens, logprobs):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:  # backend down/overloaded: the stream ends unfinished
                bt.logging.warning(f'ServingMiner backend error: {e}')
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        async with miner.backend_slots:
            producer = loop.run_in_executor(None, produce)
            while (chunk := await queue.get()) is not None:
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
            await producer

    return synapse.create_streaming_response(token_streamer)


async def handle_attest(miner: ServingMiner, synapse: AttestSynapse) -> AttestSynapse:
    """Run the validator's hardware challenge on this miner's runtime (attest sidecar, docker/attest)."""
    url = miner.release.attest_url
    if not url:
        synapse.error = 'no attest_url on the release'
        return synapse

    def call() -> dict:
        r = requests.post(
            f'{url.rstrip("/")}/v1/attest',
            json={'seed': int(synapse.seed), 'iters': int(synapse.iters), 'fill': bool(synapse.fill)},
            timeout=max(5.0, float(synapse.timeout or 45.0) - 2.0),
        )
        r.raise_for_status()
        return r.json()

    try:
        payload = await asyncio.get_running_loop().run_in_executor(None, call)
    except Exception as e:
        synapse.error = f'attest sidecar: {e!r}'[:300]
        return synapse
    devices = payload.get('devices') or [payload]
    synapse.devices = [dict(d) for d in devices]
    synapse.wall_ms = float(devices[0].get('wall_ms') or 0.0) if devices else None
    synapse.queued_ms = float(payload.get('queued_ms') or 0.0)
    return synapse


async def verify_inference(miner: ServingMiner, synapse: InferenceSynapse) -> None:
    """Signature + freshness + no-replay check without the strictly increasing nonce the default verify enforces.

    A validator gateway dispatches concurrent requests from one dendrite; they arrive out of order and the
    default verify rejects every request whose nonce is older than the newest one processed. Exact repeats of a
    (hotkey, uuid, nonce) within the freshness window are rejected instead.
    """
    d = synapse.dendrite
    if d is None or d.nonce is None or d.hotkey is None:
        raise Exception('Missing dendrite headers')
    now_ns = time.time_ns()
    if d.nonce <= allowed_nonce_window_ns(now_ns, synapse.timeout):
        diff, allowed = calculate_diff_seconds(now_ns, synapse.timeout, d.nonce)
        raise Exception(
            f'Nonce is too old: acceptable delta is {allowed:.2f} seconds but request was {diff:.2f} seconds old'
        )
    message = f'{d.nonce}.{d.hotkey}.{miner.wallet.hotkey.ss58_address}.{d.uuid}.{synapse.computed_body_hash}'
    if not d.signature or not Keypair(ss58_address=d.hotkey).verify(message, d.signature):
        raise Exception('Signature mismatch')
    key = f'{d.hotkey}:{d.uuid}:{d.nonce}'
    if key in miner.seen_nonces:
        raise Exception('Nonce replayed')
    miner.seen_nonces[key] = None
    while len(miner.seen_nonces) > SERVING_SEEN_NONCES:
        miner.seen_nonces.popitem(last=False)


def min_caller_stake() -> float:
    return float(os.getenv('SERVING_MIN_CALLER_STAKE', SERVING_MIN_CALLER_STAKE))


def validator_tokens_per_tempo() -> int:
    return int(os.getenv('SERVING_VALIDATOR_TOKENS_PER_TEMPO', SERVING_VALIDATOR_TOKENS_PER_TEMPO))


def reserve_audit_budget(miner: ServingMiner, hotkey: str, tokens: int) -> bool:
    """Charge ``tokens`` to a permitted validator's per-tempo budget; False when it would overrun."""
    tempo = int(getattr(miner.metagraph, 'block', 0) or 0) // BLOCKS_PER_TEMPO
    spent_tempo, used = miner.audit_budget.get(hotkey, (tempo, 0))
    if spent_tempo != tempo:
        used = 0
    if used + tokens > validator_tokens_per_tempo():
        return False
    miner.audit_budget[hotkey] = (tempo, used + tokens)
    return True


async def blacklist_inference(miner: ServingMiner, synapse: InferenceSynapse) -> Tuple[bool, str]:
    """Who may query: hotkeys staked at least SERVING_MIN_CALLER_STAKE alpha without limit (the gateway validator),
    and any validator-permit holder inside a per-tempo completion-token budget (an independent auditor).

    Otherwise any registered hotkey could use the miner's GPU for free inference. The floor is set so only the
    reference-running validator clears it and everyone else goes through its gateway; the permit budget keeps the
    subnet's other validators able to verify without the fleet becoming their free API.
    """
    hotkey = synapse.dendrite.hotkey if synapse.dendrite else None
    if not hotkey or hotkey not in miner.metagraph.hotkeys:
        return True, 'Unrecognized hotkey'
    if synapse.release_id and synapse.release_id != miner.release.release_id:
        return True, f'serving release {miner.release.release_id}, not {synapse.release_id}'
    try:
        uid = miner.metagraph.hotkeys.index(hotkey)
        stake = float(miner.metagraph.S[uid])
    except (ValueError, IndexError):  # metagraph mid-sync; refuse rather than guess
        return True, 'Metagraph out of date'
    if stake >= min_caller_stake():
        return False, 'Staked caller'
    permits = getattr(miner.metagraph, 'validator_permit', None)
    try:
        permitted = bool(permits[uid]) if permits is not None else False
    except (IndexError, TypeError):
        permitted = False
    if not permitted:
        return True, f'Stake {stake:.0f} below {min_caller_stake():.0f}'
    if not reserve_audit_budget(miner, hotkey, int(synapse.max_tokens)):
        return True, f'Validator audit budget spent ({validator_tokens_per_tempo()} tokens per tempo)'
    return False, 'Permitted validator'


# bt.Axon.attach asserts each hook's signature against the forward's synapse type, so the attestation hooks are
# typed AttestSynapse and delegate to the inference gate. An attestation charges no tokens to a validator's budget.
async def blacklist_attest(miner: ServingMiner, synapse: AttestSynapse) -> Tuple[bool, str]:
    return await blacklist_inference(miner, _as_budget_free(synapse))


async def priority_attest(miner: ServingMiner, synapse: AttestSynapse) -> float:
    return await priority_inference(miner, _as_budget_free(synapse))


async def verify_attest(miner: ServingMiner, synapse: AttestSynapse) -> None:
    await verify_inference(miner, synapse)  # type: ignore[arg-type]


def _as_budget_free(synapse: AttestSynapse) -> InferenceSynapse:
    """The inference gate reads dendrite + max_tokens; an attestation is the same caller charging zero tokens."""
    shim = InferenceSynapse(messages=[], model_id='', max_tokens=0)
    shim.dendrite = synapse.dendrite
    return shim


async def priority_inference(miner: ServingMiner, synapse: InferenceSynapse) -> float:
    hotkey = synapse.dendrite.hotkey if synapse.dendrite else None
    if not hotkey or hotkey not in miner.metagraph.hotkeys:
        return 0.0
    try:  # hotkeys and S are refreshed separately by metagraph.sync() on the main thread
        return float(miner.metagraph.S[miner.metagraph.hotkeys.index(hotkey)])
    except (ValueError, IndexError):
        return 0.0


def main():
    miner = ServingMiner()
    miner.run()


if __name__ == '__main__':
    main()
