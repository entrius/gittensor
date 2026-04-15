import os

# required env vars
GITTENSOR_VALIDATOR_PAT = os.getenv('GITTENSOR_VALIDATOR_PAT')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'gittensor-validators')
WANDB_VALIDATOR_NAME = os.getenv('WANDB_VALIDATOR_NAME', 'vali')

# optional env vars
STORE_DB_RESULTS = os.getenv('STORE_DB_RESULTS', 'false').lower() == 'true'
