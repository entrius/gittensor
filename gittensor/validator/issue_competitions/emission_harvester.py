# The MIT License (MIT)
# Copyright 2025 Entrius

"""Emission Harvester for contract treasury management.

This module provides the EmissionHarvester class that periodically harvests
emissions from the contract's treasury hotkey and distributes them to pending
bounties. The harvester runs alongside the validator's forward loop.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import bittensor as bt

from gittensor.validator.issue_competitions.contract_client import (
    GITTENSOR_CONFIG_PATH,
    IssueCompetitionContractClient,
    get_contract_address_from_config,
)

if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron


@dataclass
class HarvestConfig:
    """Configuration for the emission harvester."""

    enabled: bool = True
    interval_blocks: int = 1000  # ~200 minutes at 12s blocks (reduced frequency to avoid rate limiting)
    contract_address: Optional[str] = None


def get_harvest_config(config: bt.Config) -> HarvestConfig:
    """
    Extract harvest configuration from validator config and gittensor config file.

    Args:
        config: Bittensor config object

    Returns:
        HarvestConfig with values from config or defaults
    """
    # Handle nested config access safely
    contract_config = getattr(config, 'contract', None)

    # Start with defaults
    harvest_config = HarvestConfig()

    # Load from bittensor config
    if contract_config is not None:
        harvest_config.enabled = getattr(contract_config, 'harvest_emissions', True)
        harvest_config.interval_blocks = getattr(contract_config, 'harvest_interval', 1000)
        harvest_config.contract_address = getattr(contract_config, 'address', None)

    # Load contract address from gittensor config file if not set
    if not harvest_config.contract_address and GITTENSOR_CONFIG_PATH.exists():
        try:
            with open(GITTENSOR_CONFIG_PATH) as f:
                gittensor_config = json.load(f)
                harvest_config.contract_address = gittensor_config.get('contract_address')
        except (json.JSONDecodeError, IOError) as e:
            bt.logging.warning(f'Failed to read gittensor config: {e}')

    return harvest_config


class EmissionHarvester:
    """
    Harvests emissions from contract treasury.

    This class manages the periodic harvesting of emissions that accumulate
    on the contract's treasury hotkey. It can run either:
    1. As part of the validator's forward loop (call maybe_harvest each step)
    2. As a background task (call start_background_loop)

    The harvest is permissionless - anyone can call it, but we run it from
    the validator to ensure bounties are funded regularly.
    """

    def __init__(
        self,
        config: bt.Config,
        subtensor: bt.Subtensor,
        wallet: bt.Wallet,
        contract_client: Optional[IssueCompetitionContractClient] = None,
    ):
        """
        Initialize the emission harvester.

        Args:
            config: Bittensor config with harvest settings
            subtensor: Subtensor instance for chain interaction
            wallet: Wallet for signing harvest transactions
            contract_client: Optional pre-configured contract client
        """
        self.harvest_config = get_harvest_config(config)
        self.subtensor = subtensor
        self.wallet = wallet
        self.last_harvest_block = 0
        self._running = False

        # Initialize or use provided contract client
        if contract_client:
            self.contract_client = contract_client
        else:
            contract_address = (
                self.harvest_config.contract_address
                or get_contract_address_from_config()
            )
            self.contract_client = IssueCompetitionContractClient(
                contract_address=contract_address,
                subtensor=subtensor,
            )

        bt.logging.info(
            f'EmissionHarvester initialized: '
            f'enabled={self.harvest_config.enabled}, '
            f'interval={self.harvest_config.interval_blocks} blocks'
        )

    @property
    def enabled(self) -> bool:
        """Check if harvesting is enabled."""
        return self.harvest_config.enabled

    def get_current_block(self) -> int:
        """Get the current block number from subtensor."""
        try:
            return self.subtensor.get_current_block()
        except Exception as e:
            bt.logging.warning(f'Failed to get current block: {e}')
            return 0

    def should_harvest(self, current_block: int) -> bool:
        """
        Check if it's time to harvest.

        Args:
            current_block: Current block number

        Returns:
            True if harvest is due
        """
        if not self.enabled:
            return False

        blocks_since_harvest = current_block - self.last_harvest_block
        return blocks_since_harvest >= self.harvest_config.interval_blocks

    async def maybe_harvest(self, current_block: Optional[int] = None) -> Optional[dict]:
        """
        Check if harvest is due and execute if so.

        This method is designed to be called from the validator's main loop.
        It's non-blocking and returns quickly if harvest is not due.

        Args:
            current_block: Optional current block (will query if not provided)

        Returns:
            Harvest result dict if harvest was executed, None otherwise
        """
        if not self.enabled:
            return None

        if current_block is None:
            current_block = self.get_current_block()

        if not self.should_harvest(current_block):
            return None

        return await self.harvest()

    async def harvest(self) -> dict:
        """
        Execute harvest_emissions on the contract.

        The contract handles everything via chain extension:
        - Queries its own pending stake via get_stake_info (function 0)
        - Fills bounties from the pool
        - Moves bounty funds to validator via move_stake
        - Recycles remainder via transfer_stake

        Includes retry logic with exponential backoff for transient errors
        like "Transaction temporarily banned".

        Returns:
            Result dict with harvest status and amounts
        """
        max_retries = 3
        base_delay = 5  # seconds

        for attempt in range(max_retries):
            try:
                bt.logging.debug(f'Calling contract harvest_emissions... (attempt {attempt + 1}/{max_retries})')

                # Let the contract do everything - it queries stake via chain extension
                result = self.contract_client.harvest_emissions(self.wallet)

                # Always update last_harvest_block to prevent immediate retry on failure
                self.last_harvest_block = self.get_current_block()

                if result:
                    if result.get('status') == 'success':
                        bt.logging.success('Harvest complete: recycling succeeded')
                    else:
                        error_msg = result.get('error', 'Unknown error')
                        # Use warning for transient errors, error for persistent ones
                        if any(kw in error_msg.lower() for kw in ['banned', 'timeout', 'connection']):
                            bt.logging.warning(f'Harvest skipped (transient): {error_msg}')
                        else:
                            bt.logging.error(f'Harvest failed: {error_msg}')

                    return result
                else:
                    return {'status': 'error', 'error': 'No result from contract'}

            except Exception as e:
                error_str = str(e).lower()

                # Check for retryable errors (temporary bans, connection issues)
                is_retryable = (
                    'temporarily banned' in error_str
                    or 'connection' in error_str
                    or 'timeout' in error_str
                )

                if is_retryable and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    bt.logging.warning(
                        f'Harvest failed with retryable error, waiting {delay}s '
                        f'(attempt {attempt + 1}/{max_retries}): {e}'
                    )
                    await asyncio.sleep(delay)
                    continue

                bt.logging.error(f'Harvest error (attempt {attempt + 1}/{max_retries}): {e}')
                return {'status': 'error', 'error': str(e)}

        # Should not reach here, but just in case
        return {'status': 'error', 'error': 'Max retries exceeded'}

    async def start_background_loop(self):
        """
        Run harvester as a background task.

        This method runs indefinitely, harvesting at the configured interval.
        Use this if you want to run harvesting independently of the forward loop.
        """
        if not self.enabled:
            bt.logging.info('Emission harvesting disabled, background loop not starting')
            return

        bt.logging.info('Starting emission harvester background loop')
        self._running = True

        while self._running:
            try:
                result = await self.harvest()
                if result and result.get('harvested', 0) > 0:
                    bt.logging.info(f'Background harvest: {result}')
            except Exception as e:
                bt.logging.error(f'Background harvest error: {e}')

            # Wait for interval (convert blocks to approximate seconds)
            # Assuming ~250ms block time
            wait_seconds = self.harvest_config.interval_blocks * 0.25
            await asyncio.sleep(wait_seconds)

    def stop(self):
        """Stop the background harvester loop."""
        self._running = False
        bt.logging.info('Emission harvester stopped')


def create_harvester_for_validator(
    validator: 'BaseValidatorNeuron',
) -> Optional[EmissionHarvester]:
    """
    Create an EmissionHarvester configured for a validator.

    This is a convenience function to create a harvester with the
    validator's existing subtensor and wallet.

    Args:
        validator: The validator instance

    Returns:
        Configured EmissionHarvester, or None if disabled
    """
    config = validator.config
    harvest_config = get_harvest_config(config)

    if not harvest_config.enabled:
        bt.logging.info('Emission harvesting disabled in config')
        return None

    return EmissionHarvester(
        config=config,
        subtensor=validator.subtensor,
        wallet=validator.wallet,
    )
