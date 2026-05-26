import os

import bittensor as bt

VALIDATOR_WAIT = 60  # 60 seconds
VALIDATOR_STEPS_INTERVAL = 120  # 2 hours, every time a scoring round happens

# How many miners to evaluate concurrently. Each evaluation is network-bound on
# the mirror; scoring them in parallel overlaps that latency. The cap keeps
# in-flight requests within the mirror's 50 req / 10s rate limit (the client
# still backs off on 429 as a safety net). Override with MINER_EVALUATION_CONCURRENCY.
MINER_EVALUATION_CONCURRENCY = max(1, int(os.getenv('MINER_EVALUATION_CONCURRENCY', '8')))

# required env vars
GITTENSOR_VALIDATOR_PAT = os.getenv('GITTENSOR_VALIDATOR_PAT')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'gittensor-validators')
WANDB_VALIDATOR_NAME = os.getenv('WANDB_VALIDATOR_NAME', 'vali')

# optional env vars
STORE_DB_RESULTS = os.getenv('STORE_DB_RESULTS', 'false').lower() == 'true'

# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'MINER_EVALUATION_CONCURRENCY: {MINER_EVALUATION_CONCURRENCY}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
