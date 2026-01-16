# Entrius 2025
from typing import List, Optional

import bittensor as bt


class GitPatSynapse(bt.Synapse):
    """
    This synapse is used to request GitHub access tokens from a miner and receive the response.

    Attributes:
    - github_access_token: A string value representing the GitHub access token.
      Initially None for requests, and set to the actual token for responses.
    - issue_preferences: Ranked list of issue IDs the miner wants to compete on.
      Most preferred first. Max 5 preferences. Empty list = not interested
      in issue competitions. Miner reads from ~/.gittensor/issue_preferences.json
    """

    github_access_token: Optional[str] = None

    # Issue competition preferences (ranked by preference, most preferred first)
    # Empty list means miner is not interested in issue competitions
    issue_preferences: List[int] = []
