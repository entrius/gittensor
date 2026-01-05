import os

import bittensor as bt

# NOTE: bump this number when we make new updates
__version__ = '3.0.2'


VALIDATOR_WAIT = 60  # 60 seconds
VALIDATOR_STEPS_INTERVAL = 120  # 2 hours, every time a scoring round happens
PR_LOOKBACK_DAYS = 90  # how many days a merged pr will count for scoring

# required env vars
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'gittensor-validators')

# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'PR_LOOKBACK_DAYS: {PR_LOOKBACK_DAYS}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
