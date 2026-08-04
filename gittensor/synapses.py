# Entrius 2025
from typing import Optional

import bittensor as bt
from pydantic import Field


class PatBroadcastSynapse(bt.Synapse):
    """Miner-initiated push synapse to broadcast their GitHub PAT to validators.

    The miner sets github_access_token on the request. The validator validates the PAT
    (checks it works, extracts GitHub ID, runs a test query)
    and responds with accepted/rejection_reason.
    """

    # Miner request. repr=False keeps pydantic's default repr from emitting the
    # raw token; the explicit __repr__/__str__ below render a last-4-char tag so
    # masked log lines remain correlatable with rotated tokens.
    github_access_token: str = Field(repr=False)

    # Validator response
    accepted: Optional[bool] = None
    rejection_reason: Optional[str] = None

    def __repr__(self) -> str:
        token = self.github_access_token or ''
        masked = f'***{token[-4:]}' if len(token) >= 4 else '***'
        return (
            f'PatBroadcastSynapse(github_access_token={masked}, '
            f'accepted={self.accepted!r}, rejection_reason={self.rejection_reason!r})'
        )

    __str__ = __repr__


class InferenceSynapse(bt.Synapse):
    """Validator-initiated inference challenge for serving miners (sub-subnet B beta).

    The validator sets the request fields; the serving miner runs its pinned
    backend and fills the response fields. With a deterministic backend the
    validator knows the exact expected completion, so correctness is a string
    match and latency comes from dendrite process time.
    """

    # Validator request
    prompt: str
    model_id: str
    max_tokens: int = 64

    # Miner response
    completion: Optional[str] = None
    served_model_id: Optional[str] = None
    generation_ms: Optional[float] = None


class PatCheckSynapse(bt.Synapse):
    """Probe for miners to check if a validator has their PAT stored and valid.

    No PAT is sent — the validator identifies the miner by their dendrite hotkey,
    looks up the stored PAT, and re-validates it (GitHub API check + test query).
    """

    # Validator response
    has_pat: Optional[bool] = None
    pat_valid: Optional[bool] = None
    rejection_reason: Optional[str] = None
