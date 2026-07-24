import os

import bittensor as bt

VALIDATOR_WAIT = 60  # 60 seconds
VALIDATOR_STEPS_INTERVAL = 120  # 2 hours, every time a scoring round happens

# Weight consensus: validator-voted repository emission shares via chain commitments.
# Tunables below are provisional team defaults — each is a one-line change.
CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS = 3600  # ~12h @ 12s/block, aggregate recomputed 2x/day
CONSENSUS_MIN_VALIDATOR_STAKE_RAO = 30_000 * 10**9  # 30k alpha voter threshold
CONSENSUS_MAX_REPOS = 10  # max repos per validator basket
CONSENSUS_MAX_PAYLOAD_BYTES = 512  # chain BigRaw field limit
CONSENSUS_FRESH_WINDOW_BLOCKS = 200  # lite nodes prune state ~256 blocks back
CONSENSUS_CACHE_KEEP = 8  # snapshots retained in the disk cache
CONSENSUS_WEIGHT_PRECISION = 10**12  # fixed-point scale for deterministic int math

# required env vars
GITTENSOR_VALIDATOR_PAT = os.getenv('GITTENSOR_VALIDATOR_PAT')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'gittensor-validators')
WANDB_VALIDATOR_NAME = os.getenv('WANDB_VALIDATOR_NAME', 'vali')

# optional env vars
STORE_DB_RESULTS = os.getenv('STORE_DB_RESULTS', 'false').lower() == 'true'

# Mirror reconciliation (team validator only; disabled without the API key)
MIRROR_ADMIN_API_KEY = os.getenv('MIRROR_ADMIN_API_KEY', '')
MIRROR_DEREG_SNAPSHOTS = 4  # consecutive absent snapshots (~2 days) before deregistration
MIRROR_MAX_TRACKED_REPOS = 300  # hard cap on repos synced to the mirror
MIRROR_BACKFILL_DAYS = 40  # deep backfill window for newly registered repos

# log values
bt.logging.info(f'VALIDATOR_WAIT: {VALIDATOR_WAIT}')
bt.logging.info(f'VALIDATOR_STEPS_INTERVAL: {VALIDATOR_STEPS_INTERVAL}')
bt.logging.info(f'WANDB_PROJECT: {WANDB_PROJECT}')
