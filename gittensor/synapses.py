# Entrius 2025
from typing import Optional

import bittensor as bt


class PatBroadcastSynapse(bt.Synapse):
    """Miner-initiated push synapse to broadcast their GitHub PAT to validators.

    The miner sets github_access_token on the request. The validator validates the PAT
    (checks it works, extracts GitHub ID, verifies account age, runs a test query)
    and responds with accepted/rejection_reason.
    """

    # Miner request
    github_access_token: str

    # Validator response
    accepted: Optional[bool] = None
    rejection_reason: Optional[str] = None


class PatCheckSynapse(bt.Synapse):
    """Lightweight probe for miners to check if a validator has their PAT stored.

    No PAT is sent — the validator identifies the miner by their dendrite hotkey
    and checks local storage.
    """

    # Validator response
    has_pat: Optional[bool] = None
