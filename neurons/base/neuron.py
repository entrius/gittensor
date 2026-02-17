# The MIT License (MIT)
# Copyright © 2023 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import copy
import time
from abc import ABC, abstractmethod
from typing import Optional

import bittensor as bt
from websockets.exceptions import ConnectionClosedError

from gittensor import __spec_version__ as spec_version
from gittensor.mock import MockMetagraph, MockSubtensor

# Sync calls set weights and also resyncs the metagraph.
from gittensor.utils.config import add_args, check_config, config
from gittensor.utils.misc import ttl_get_block


class BaseNeuron(ABC):
    """
    Base class for Bittensor miners. This class is abstract and should be inherited by a subclass. It contains the core logic for all neurons; validators and miners.

    In addition to creating a wallet, subtensor, and metagraph, this class also handles the synchronization of the network state via a basic checkpointing mechanism based on epoch length.
    """

    neuron_type: str = 'BaseNeuron'

    @classmethod
    def check_config(cls, config: 'bt.Config') -> None:
        """Validate the provided configuration object.

        Args:
            config: The Bittensor configuration object to validate.
        """
        check_config(cls, config)

    @classmethod
    def add_args(cls, parser: 'bt.ArgumentParser') -> None:
        """Add neuron-specific arguments to the argument parser.

        Args:
            parser: The argument parser to add neuron arguments to.
        """
        add_args(cls, parser)

    @classmethod
    def config(cls) -> 'bt.Config':
        """Create and return the default configuration for this neuron.

        Returns:
            A Bittensor configuration object with default values.
        """
        return config(cls)

    subtensor: 'bt.subtensor'
    wallet: 'bt.wallet'
    metagraph: 'bt.metagraph'
    spec_version: int = spec_version

    @property
    def block(self) -> int:
        """Get the current block number from the network.

        Returns:
            The current block number, cached with a TTL to reduce RPC calls.
        """
        return ttl_get_block(self)

    def __init__(self, config: Optional['bt.Config'] = None) -> None:
        """Initialize the base neuron.

        Sets up the wallet, subtensor connection, metagraph, and verifies
        registration on the Bittensor network.

        Args:
            config: Configuration object for the neuron. If None, uses default config.
        """
        base_config = copy.deepcopy(config or BaseNeuron.config())
        self.config = self.config()
        self.config.merge(base_config)
        self.check_config(self.config)

        # Set up logging with the provided configuration.
        bt.logging.set_config(config=self.config.logging)

        # If a gpu is required, set the device to cuda:N (e.g. cuda:0)
        self.device = self.config.neuron.device

        # Log the configuration for reference.
        # bt.logging.info(self.config)

        # Build Bittensor objects
        # These are core Bittensor classes to interact with the network.
        bt.logging.info('Setting up bittensor objects.')

        # The wallet holds the cryptographic key pairs for the miner.
        if self.config.mock:
            self.wallet = bt.MockWallet(config=self.config)
            self.subtensor = MockSubtensor(self.config.netuid, wallet=self.wallet)
            self.metagraph = MockMetagraph(self.config.netuid, subtensor=self.subtensor)
        else:
            self.wallet = bt.wallet(config=self.config)
            self.subtensor = bt.subtensor(config=self.config)
            self.metagraph = self.subtensor.metagraph(self.config.netuid)

        bt.logging.info(f'Wallet: {self.wallet}')
        bt.logging.info(f'Subtensor: {self.subtensor}')
        bt.logging.info(f'Metagraph: {self.metagraph}')

        # Check if the miner is registered on the Bittensor network before proceeding further.
        self.check_registered()

        # Each miner gets a unique identity (UID) in the network for differentiation.
        self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        bt.logging.info(
            f'Running neuron on subnet: {self.config.netuid} with uid {self.uid} using network: {self.subtensor.chain_endpoint}'
        )
        self.step = 0

    def _reconnect_subtensor(self) -> None:
        """Recreate subtensor connection when WebSocket goes stale.

        Reinitializes the subtensor connection to recover from dropped
        or stale WebSocket connections. Skipped in mock mode.
        """
        if self.config.mock:
            return  # Don't reconnect in mock mode
        bt.logging.info('Reconnecting subtensor...')
        self.subtensor = bt.subtensor(config=self.config)

    @abstractmethod
    async def forward(self, synapse: bt.Synapse) -> bt.Synapse: ...

    @abstractmethod
    def run(self) -> None: ...

    def sync(self) -> None:
        """
        Wrapper for synchronizing the state of the network for the given miner or validator.

        This method performs the following steps:
        1. Checks that the neuron is still registered on the network
        2. Resyncs the metagraph if enough epoch blocks have elapsed
        3. Sets weights if the conditions are met
        4. Saves the current state
        """
        # Ensure miner or validator hotkey is still registered on the network.
        self.check_registered()

        if self.should_sync_metagraph():
            self.resync_metagraph()

        if self.should_set_weights():
            self.set_weights()

        # Always save state.
        self.save_state()

    def check_registered(self, max_retries: int = 3) -> None:
        """Check if hotkey is registered on the network, with retry logic for connection failures.

        Verifies that this neuron's hotkey is registered on the configured subnet.
        Implements exponential backoff retry logic to handle transient WebSocket
        connection failures.

        Args:
            max_retries: Maximum number of retry attempts for connection failures.

        Raises:
            ConnectionClosedError: If all retry attempts are exhausted due to
                WebSocket connection failures.
            SystemExit: If the hotkey is not registered on the network.
        """
        for attempt in range(max_retries):
            try:
                if not self.subtensor.is_hotkey_registered(
                    netuid=self.config.netuid,
                    hotkey_ss58=self.wallet.hotkey.ss58_address,
                ):
                    bt.logging.error(
                        f'Wallet: {self.wallet} is not registered on netuid {self.config.netuid}.'
                        f' Please register the hotkey using `btcli subnets register` before trying again'
                    )
                    exit()
                return  # Success
            except ConnectionClosedError as e:
                bt.logging.warning(
                    f'WebSocket connection closed during check_registered (attempt {attempt + 1}/{max_retries}): {e}'
                )
                if attempt < max_retries - 1:
                    self._reconnect_subtensor()
                    time.sleep(2**attempt)  # Exponential backoff: 1s, 2s, 4s
                else:
                    raise

    def should_sync_metagraph(self) -> bool:
        """
        Check if enough epoch blocks have elapsed since the last checkpoint to sync.

        Returns:
            True if the metagraph should be resynced, False otherwise.
        """
        return (self.block - self.metagraph.last_update[self.uid]) > self.config.neuron.epoch_length

    def should_set_weights(self) -> bool:
        """Determine whether the neuron should set weights on the network.

        Weights are not set on initialization (step 0), when weight setting
        is disabled, or when the neuron is a miner. Validators set weights
        after enough epoch blocks have elapsed.

        Returns:
            True if weights should be set, False otherwise.
        """
        # Don't set weights on initialization.
        if self.step == 0:
            return False

        # Check if enough epoch blocks have elapsed since the last epoch.
        if self.config.neuron.disable_set_weights:
            return False

        # Define appropriate logic for when set weights.
        return (
            self.block - self.metagraph.last_update[self.uid]
        ) > self.config.neuron.epoch_length and self.neuron_type != 'MinerNeuron'  # don't set weights if you're a miner

    def save_state(self) -> None:
        """Save the state of the neuron to a file.

        This method should be overridden by subclasses to implement
        neuron-specific state persistence (e.g., model checkpoints).
        """
        bt.logging.trace(
            'save_state() not implemented for this neuron. You can implement this function to save model checkpoints or other useful data.'
        )

    def load_state(self) -> None:
        """Load the state of the neuron from a file.

        This method should be overridden by subclasses to implement
        neuron-specific state loading (e.g., model checkpoints).
        """
        bt.logging.trace(
            'load_state() not implemented for this neuron. You can implement this function to load model checkpoints or other useful data.'
        )
