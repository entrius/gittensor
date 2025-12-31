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


import threading
import time
from typing import Dict

import bittensor as bt
import wandb

from gittensor.classes import MinerEvaluation
from gittensor.validator.forward import forward
from gittensor.validator.utils.config import WANDB_PROJECT, __version__
from gittensor.validator.utils.storage import DatabaseStorage
from neurons.base.validator import BaseValidatorNeuron


class Validator(BaseValidatorNeuron):
    """
    This class inherits from the BaseValidatorNeuron class, which in turn inherits from BaseNeuron.
    The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc.
    You can override any of the methods in BaseNeuron if you need to customize the behavior.
    """

    db_storage: DatabaseStorage = None

    def __init__(self, config=None):
        super(Validator, self).__init__(config=config)

        # Init DB for validation result storage. Requires STORE_DB_RESULTS in .env
        if self.config.database.store_validation_results:
            bt.logging.warning("Validation result storage enabled.")
            self.db_storage = DatabaseStorage()

        # Init remote debugging API (FOR DEVELOPMENT ONLY). Requires DEBUGPY_PORT in .env
        if self.config.neuron.remote_debug_port is not None:
            from gittensor.validator.test.live_testnet.test_validator_live import start_debug_api

            bt.logging.warning("Remote debugging api enabled.")

            # Start debug API in background thread
            debug_thread = threading.Thread(
                target=start_debug_api,
                args=(self, self.config.neuron.remote_debug_port),
                daemon=True,
                name="RemoteDebugAPI",
            )
            debug_thread.start()

        # Initialize wandb only if disable_set_weights is False
        if not self.config.neuron.disable_set_weights:
            try:
                wandb.init(
                    entity="entrius-gittensor",
                    project=WANDB_PROJECT,
                    name=f"vali-{self.uid}-{__version__}",
                    config=self.config,
                    reinit=True,
                )
            except Exception as e:
                bt.logging.error(f"Failed to initialize wandb run: {e}")

        bt.logging.info("load_state()")
        self.load_state()

    async def bulk_store_evaluation(self, miner_evals: Dict[int, MinerEvaluation]):
        """
        Wrapper function to store all miner evaluations at once.
        """

        if self.db_storage is not None:
            for uid, evaluation in miner_evals.items():
                await self.store_evaluation(uid, evaluation)

    async def store_evaluation(self, uid: int, miner_eval: MinerEvaluation):
        """
        Stores the miner eval if DB storage is enabled by validator via --database.store_validation_results flag.
        """

        if self.db_storage is not None:
            try:
                storage_result = self.db_storage.store_evaluation(miner_eval)

                if storage_result.success:
                    bt.logging.success(f"Successfully stored validation results for UID {uid} to DB.")
                else:
                    bt.logging.warning(f"Storage partially failed for UID {uid}:")
                    for error in storage_result.errors:
                        bt.logging.warning(f"  - {error}")

            except Exception as e:
                bt.logging.error(f"Error when attempting to store miners evaluation for uid {uid}: {e}")

    async def forward(self):
        """
        Validator forward pass. Consists of:
        - Generating the query
        - Querying the miners
        - Getting the responses
        - Rewarding the miners
        - Updating the scores
        """
        return await forward(self)


def main():
    with Validator() as validator:
        while True:
            bt.logging.info(f"Validator running | uid {validator.uid} | {time.time()}")
            time.sleep(30)
            # Check after initial sleep in-case there's startup delay
            if not validator.thread.is_alive():
                bt.logging.error(f"Validator thread is not alive. Exiting...")
                break  # exit, trigger restart


if __name__ == "__main__":
    main()
