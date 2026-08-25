import os

import bittensor as bt

from gittensor.constants import SERVING_API_DEFAULT_PORT

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
SERVING_ENABLED = os.getenv('SERVING_ENABLED', 'false').lower() == 'true'
SERVING_STEPS_INTERVAL = int(os.getenv('SERVING_STEPS_INTERVAL', '5'))  # steps (~minutes) between serving audit rounds
if VALIDATOR_STEPS_INTERVAL < 1 or SERVING_STEPS_INTERVAL < 1:
    raise ValueError('VALIDATOR_STEPS_INTERVAL and SERVING_STEPS_INTERVAL must be >= 1')
# Serving inference API: off unless keys are set; loopback by default (0.0.0.0 inside docker), front it with the host proxy.
SERVING_API_KEYS = os.getenv('SERVING_API_KEYS', '')
SERVING_API_HOST = os.getenv('SERVING_API_HOST', '127.0.0.1')
SERVING_API_PORT = int(os.getenv('SERVING_API_PORT', str(SERVING_API_DEFAULT_PORT)))

# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
bt.logging.info(f'SERVING_ENABLED: {SERVING_ENABLED}')
bt.logging.info(f'SERVING_STEPS_INTERVAL: {SERVING_STEPS_INTERVAL}')
