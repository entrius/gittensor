# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Issue preferences management for miners.

This module handles loading issue preferences from the local config file
that miners set via the CLI. The preferences are served to validators
via the GitPatSynapse.

The preferences file is located at: ~/.gittensor/issue_preferences.json
"""

import json
from pathlib import Path
from typing import List

import bittensor as bt


# Path to the issue preferences file
GITTENSOR_DIR = Path.home() / '.gittensor'
ISSUE_PREFERENCES_FILE = GITTENSOR_DIR / 'issue_preferences.json'

# Maximum number of preferences to serve
MAX_PREFERENCES = 5


def load_issue_preferences() -> List[int]:
    """
    Load miner's issue preferences from local config file.

    The preferences file is created by the CLI command:
        gitt issue prefer <id1> <id2> ...

    Returns:
        List of issue IDs in preference order (most preferred first).
        Returns empty list if no preferences set or file doesn't exist.
    """
    if not ISSUE_PREFERENCES_FILE.exists():
        bt.logging.trace('No issue preferences file found')
        return []

    try:
        with open(ISSUE_PREFERENCES_FILE, 'r') as f:
            data = json.load(f)

        preferences = data.get('preferences', [])

        # Validate and limit
        valid_prefs = []
        for p in preferences[:MAX_PREFERENCES]:
            if isinstance(p, int) and p > 0:
                valid_prefs.append(p)

        if valid_prefs:
            bt.logging.debug(f'Loaded issue preferences: {valid_prefs}')

        return valid_prefs

    except json.JSONDecodeError as e:
        bt.logging.warning(f'Invalid issue preferences file format: {e}')
        return []
    except IOError as e:
        bt.logging.warning(f'Error reading issue preferences file: {e}')
        return []


def has_preferences() -> bool:
    """Check if miner has any issue preferences set."""
    return len(load_issue_preferences()) > 0


def get_preferences_file_path() -> Path:
    """Get the path to the issue preferences file."""
    return ISSUE_PREFERENCES_FILE
