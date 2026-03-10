# Entrius 2025
from typing import Optional

import bittensor as bt


class GitPatSynapse(bt.Synapse):
    """
    This synapse is used to request GitHub access tokens from a miner and receive the response.

    Attributes:
    - github_access_token: A string value representing the GitHub access token.
      Initially None for requests, and set to the actual token for responses.
    """

    github_access_token: Optional[str] = None


class PredictionSynapse(bt.Synapse):
    """Miner-initiated push synapse for merge predictions.

    Request fields (set by miner):
    - github_access_token: Miner's GitHub PAT for identity verification and account age check.
    - issue_id: On-chain issue ID (NOT GitHub issue number).
    - repository: Full repo name, e.g. "entrius/gittensor".
    - predictions: Mapping of PR number -> probability (0.0-1.0).
      Sum across all of a miner's predictions for an issue must be <= 1.0.
      Each submission can contain one or many PR predictions.
      Submitting a prediction for a PR that already has one overwrites it.

    Response fields (set by validator):
    - accepted: Whether the prediction was stored.
    - rejection_reason: Human-readable reason if rejected.
    """

    # Miner Request
    github_access_token: str
    issue_id: int
    repository: str
    predictions: dict[int, float]

    # Validator Response
    accepted: Optional[bool] = None
    rejection_reason: Optional[str] = None
