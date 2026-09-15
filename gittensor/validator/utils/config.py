import os

import bittensor as bt

VALIDATOR_WAIT = 60  # 60 seconds
VALIDATOR_STEPS_INTERVAL = int(
    os.getenv('VALIDATOR_STEPS_INTERVAL', '120')
)  # steps (~minutes) between scoring rounds; 120 = 2 hours

# required env vars
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'gittensor-validators')
WANDB_VALIDATOR_NAME = os.getenv('WANDB_VALIDATOR_NAME', 'vali')

# optional env vars
STORE_DB_RESULTS = os.getenv('STORE_DB_RESULTS', 'false').lower() == 'true'
if VALIDATOR_STEPS_INTERVAL < 1:
    raise ValueError('VALIDATOR_STEPS_INTERVAL must be >= 1')
# Compute pool (vault 23 §8a, 26 §1): the controller's signed scorecard (`<controller state>/scorecard/latest.json`).
# Unset: the compute share recycles. Set: the validator signs and commits its sha256 and pays the compute share from
# its weights; a stale or invalid scorecard recycles the compute share.
COMPUTE_SCORECARD_PATH = os.getenv('COMPUTE_SCORECARD_PATH', '')

# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
