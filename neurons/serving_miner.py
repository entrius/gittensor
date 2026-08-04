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

Compute-miner archetype: serve inference for the current loadout, answer
validator challenges, get scored. Run it like the validator entrypoint:

    python neurons/serving_miner.py --netuid 74 --wallet.name miner --wallet.hotkey default \\
        --subtensor.network local --axon.port 8091

The backend comes from the shared serving loadout (echo = deterministic
GPU-free mock for localnet; openai-compat = a local sparkinfer/vLLM server).
Miners run this blessed neuron unmodified — serving reward is availability
and correctness based, so there is nothing to gain by editing it.
"""

import time
from functools import partial
from typing import Tuple

import bittensor as bt

from gittensor.serving.backends import InferenceBackend, load_backend
from gittensor.serving.loadout import load_serving_loadout
from gittensor.synapses import InferenceSynapse
from neurons.base.neuron import BaseNeuron


class ServingMiner(BaseNeuron):
    """Serves the loadout model over an axon and answers inference challenges."""

    neuron_type: str = 'MinerNeuron'

    def __init__(self, config=None):
        super(ServingMiner, self).__init__(config=config)

        self.loadout = load_serving_loadout()
        self.backend: InferenceBackend = load_backend(self.loadout)

        self.axon = bt.Axon(wallet=self.wallet, config=self.config)
        self.axon.attach(
            forward_fn=partial(handle_inference, self),
            blacklist_fn=partial(blacklist_inference, self),
            priority_fn=partial(priority_inference, self),
        )
        bt.logging.info(f'ServingMiner axon: {self.axon}')

    async def forward(self, synapse: bt.Synapse) -> bt.Synapse:
        # Satisfies the BaseNeuron ABC; axon requests route through handle_inference.
        return synapse

    def run(self):
        self.subtensor.serve_axon(netuid=self.config.netuid, axon=self.axon)
        self.axon.start()
        bt.logging.info(f'ServingMiner serving | uid {self.uid} | model {self.loadout.model_id}')

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


async def handle_inference(miner: ServingMiner, synapse: InferenceSynapse) -> InferenceSynapse:
    """Generate a completion for a challenge (or, later, real traffic)."""
    result = miner.backend.generate(synapse.prompt, synapse.max_tokens)
    synapse.completion = result.completion
    synapse.served_model_id = result.model_id
    synapse.generation_ms = result.generation_ms
    return synapse


async def blacklist_inference(miner: ServingMiner, synapse: InferenceSynapse) -> Tuple[bool, str]:
    """Only registered hotkeys may query; validators are the expected callers."""
    hotkey = synapse.dendrite.hotkey if synapse.dendrite else None
    if not hotkey or hotkey not in miner.metagraph.hotkeys:
        return True, 'Unrecognized hotkey'
    return False, 'Registered hotkey'


async def priority_inference(miner: ServingMiner, synapse: InferenceSynapse) -> float:
    hotkey = synapse.dendrite.hotkey if synapse.dendrite else None
    if not hotkey or hotkey not in miner.metagraph.hotkeys:
        return 0.0
    uid = miner.metagraph.hotkeys.index(hotkey)
    return float(miner.metagraph.S[uid])


def main():
    miner = ServingMiner()
    miner.run()


if __name__ == '__main__':
    main()
