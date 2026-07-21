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

_TRUTHY_ENV_VALUES = frozenset({'1', 'true', 'yes', 'on'})


def dev_mode_enabled() -> bool:
    """Whether DEV_MODE bypasses the maintainer filters.

    Read at call time rather than import time so tests and operators can toggle it
    per process. Matched by value, not presence: DEV_MODE=false must disable the
    bypass on a production validator.
    """
    return os.getenv('DEV_MODE', '').strip().lower() in _TRUTHY_ENV_VALUES


# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
