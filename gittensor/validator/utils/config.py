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
# Where the validator writes its commit record (`validator_commit.json`: the sha256 it committed, its signature):
# under its OWN state, never the controller's scorecard directory, which is read-only input on a shared host (9/16
# soak: the write failed every cycle). Unset: `<neuron full path>/validator_commit.json`, the directory the validator
# already keeps state.npz in, or ~/.bittensor/gittensor/validator_commit.json when it has none.
COMPUTE_COMMIT_PATH = os.getenv('COMPUTE_COMMIT_PATH', '')

# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
