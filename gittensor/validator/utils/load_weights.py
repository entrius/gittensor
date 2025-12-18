import json
from pathlib import Path
from typing import Any, Dict

import bittensor as bt


def load_master_repo_weights() -> Dict[str, Dict[str, Any]]:
    """
    Load repository weights from the local JSON file.
    Normalizes repository names to lowercase for case-insensitive matching.

    Returns:
        Dictionary mapping normalized (lowercase) fullName (str) to repository data dict containing:
        - weight (float): Repository weight
        Returns empty dict on error.
    """
    weights_file = Path(__file__).parent.parent / "weights" / "master_repositories.json"

    try:
        with open(weights_file, 'r') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            bt.logging.error(f"Expected dict from {weights_file}, got {type(data)}")
            return {}

        # Normalize all keys to lowercase for case-insensitive matching
        normalized_data = {repo_name.lower(): metadata for repo_name, metadata in data.items()}

        bt.logging.debug(f"Successfully loaded {len(normalized_data)} repository entries from {weights_file}")
        return normalized_data

    except FileNotFoundError:
        bt.logging.error(f"Weights file not found: {weights_file}")
        return {}
    except json.JSONDecodeError as e:
        bt.logging.error(f"Failed to parse JSON from {weights_file}: {e}")
        return {}
    except Exception as e:
        bt.logging.error(f"Unexpected error loading repository weights: {e}")
        return {}


def load_programming_language_weights() -> Dict[str, float]:
    """
    Load programming language weights from the local JSON file.

    Returns:
        Dictionary mapping extension (str) to weight (float).
        Returns empty dict on error.
    """
    weights_file = Path(__file__).parent.parent / "weights" / "programming_languages.json"

    try:
        with open(weights_file, 'r') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            bt.logging.error(f"Expected dict from {weights_file}, got {type(data)}")
            return {}

        # Validate that all values are numeric
        result = {}
        for extension, weight in data.items():
            try:
                result[extension] = float(weight)
            except (ValueError, TypeError) as e:
                bt.logging.warning(f"Could not convert weight to float for {extension}: {weight} - {e}")
                continue

        bt.logging.debug(f"Successfully loaded {len(result)} language entries from {weights_file}")
        return result

    except FileNotFoundError:
        bt.logging.error(f"Weights file not found: {weights_file}")
        return {}
    except json.JSONDecodeError as e:
        bt.logging.error(f"Failed to parse JSON from {weights_file}: {e}")
        return {}
    except Exception as e:
        bt.logging.error(f"Unexpected error loading language weights: {e}")
        return {}
