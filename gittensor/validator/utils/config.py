"""Validator runtime settings from the process environment.

Use this module for validator-only values: ``GITTENSOR_VALIDATOR_PAT``, ``STORE_DB_RESULTS``,
``WANDB_*`` defaults, and scoring-loop timing (``VALIDATOR_WAIT``, ``VALIDATOR_STEPS_INTERVAL``).

For neuron argparse and shared ``bt.Config`` (wallet, subtensor, ``--neuron.*`` flags), use
``gittensor.utils.config`` instead.

Importing this module does not log or perform other side effects; call
``log_validator_runtime_settings()`` once from ``Validator.__init__`` if you want startup logs.
"""

import os

import bittensor as bt

VALIDATOR_WAIT = 60  # 60 seconds
VALIDATOR_STEPS_INTERVAL = 120  # 2 hours, every time a scoring round happens

# required env vars
GITTENSOR_VALIDATOR_PAT = os.getenv('GITTENSOR_VALIDATOR_PAT')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'gittensor-validators')
WANDB_VALIDATOR_NAME = os.getenv('WANDB_VALIDATOR_NAME', 'vali')

# optional env vars
STORE_DB_RESULTS = os.getenv('STORE_DB_RESULTS', 'false').lower() == 'true'


def log_validator_runtime_settings() -> None:
    """Log timing and WandB defaults once when the validator process starts."""
    bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
    bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
    bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
    bt.logging.info(f'STORE_DB_RESULTS: {STORE_DB_RESULTS}')
    bt.logging.info(f'GITTENSOR_VALIDATOR_PAT set: {bool(GITTENSOR_VALIDATOR_PAT)}')
