# Entrius 2025
from typing import Optional

import bittensor as bt
from pydantic import Field


def _mask_pat(token: str) -> str:
    """Mask a GitHub PAT for display, keeping the last 4 chars for log correlation."""
    if not token:
        return '***'
    return f'***{token[-4:]}' if len(token) >= 4 else '***'


class PatBroadcastSynapse(bt.Synapse):
    """Miner-initiated push synapse to broadcast their GitHub PAT to validators.

    The miner sets github_access_token on the request. The validator validates the PAT
    (checks it works, extracts GitHub ID, verifies account age, runs a test query)
    and responds with accepted/rejection_reason.

    The PAT is excluded from repr/str output to prevent log leaks. The wire
    format (model_dump_json) is unchanged — the token still transmits to validators.
    """

    # Miner request — repr=False so logging the synapse never leaks the PAT
    github_access_token: str = Field(repr=False)

    # Validator response
    accepted: Optional[bool] = None
    rejection_reason: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f'PatBroadcastSynapse(github_access_token={_mask_pat(self.github_access_token)}, '
            f'accepted={self.accepted!r}, rejection_reason={self.rejection_reason!r})'
        )

    __str__ = __repr__


class PatCheckSynapse(bt.Synapse):
    """Probe for miners to check if a validator has their PAT stored and valid.

    No PAT is sent — the validator identifies the miner by their dendrite hotkey,
    looks up the stored PAT, and re-validates it (GitHub API check + test query).
    """

    # Validator response
    has_pat: Optional[bool] = None
    pat_valid: Optional[bool] = None
    rejection_reason: Optional[str] = None
