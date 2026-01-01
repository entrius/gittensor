import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt

from gittensor.validator.configurations.tier_config import Tier


@dataclass
class RepositoryConfig:
    """Configuration for a repository in the master_repositories list.

    Attributes:
        weight: Repository weight for scoring
        inactive_at: ISO timestamp when repository became inactive (None if active)
        additional_acceptable_branches: List of additional branch patterns to accept (None if only default branch)
        tier: Repository tier (Bronze, Silver, Gold) - None if not assigned
    """

    weight: float
    inactive_at: Optional[str] = None
    additional_acceptable_branches: Optional[List[str]] = None
    tier: Optional[Tier] = None


def load_master_repo_weights() -> Dict[str, RepositoryConfig]:
    """
    Load repository weights from the local JSON file.
    Normalizes repository names to lowercase for case-insensitive matching.

    Returns:
        Dictionary mapping normalized (lowercase) fullName (str) to RepositoryConfig object.
        Returns empty dict on error.
    """
    weights_file = Path(__file__).parent.parent / 'weights' / 'master_repositories.json'

    try:
        with open(weights_file, 'r') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            bt.logging.error(f'Expected dict from {weights_file}, got {type(data)}')
            return {}

        # Parse JSON data into RepositoryConfig objects
        normalized_data: Dict[str, RepositoryConfig] = {}
        for repo_name, metadata in data.items():
            try:
                # Extract tier if present, convert to Tier enum
                tier_str = metadata.get('tier')
                tier = Tier(tier_str) if tier_str else None

                # Create RepositoryConfig object
                config = RepositoryConfig(
                    weight=float(metadata.get('weight', 0.01)),
                    inactive_at=metadata.get('inactive_at'),
                    additional_acceptable_branches=metadata.get('additional_acceptable_branches'),
                    tier=tier,
                )
                normalized_data[repo_name.lower()] = config
            except (ValueError, TypeError) as e:
                bt.logging.warning(f'Could not parse config for {repo_name}: {e}, using defaults')
                # Create config with defaults if parsing fails
                normalized_data[repo_name.lower()] = RepositoryConfig(weight=float(metadata.get('weight', 0.01)))

        bt.logging.debug(f'Successfully loaded {len(normalized_data)} repository entries from {weights_file}')
        return normalized_data

    except FileNotFoundError:
        bt.logging.error(f'Weights file not found: {weights_file}')
        return {}
    except json.JSONDecodeError as e:
        bt.logging.error(f'Failed to parse JSON from {weights_file}: {e}')
        return {}
    except Exception as e:
        bt.logging.error(f'Unexpected error loading repository weights: {e}')
        return {}


def load_programming_language_weights() -> Dict[str, float]:
    """
    Load programming language weights from the local JSON file.

    Returns:
        Dictionary mapping extension (str) to weight (float).
        Returns empty dict on error.
    """
    weights_file = Path(__file__).parent.parent / 'weights' / 'programming_languages.json'

    try:
        with open(weights_file, 'r') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            bt.logging.error(f'Expected dict from {weights_file}, got {type(data)}')
            return {}

        # Validate that all values are numeric
        result = {}
        for extension, weight in data.items():
            try:
                result[extension] = float(weight)
            except (ValueError, TypeError) as e:
                bt.logging.warning(f'Could not convert weight to float for {extension}: {weight} - {e}')
                continue

        bt.logging.debug(f'Successfully loaded {len(result)} language entries from {weights_file}')
        return result

    except FileNotFoundError:
        bt.logging.error(f'Weights file not found: {weights_file}')
        return {}
    except json.JSONDecodeError as e:
        bt.logging.error(f'Failed to parse JSON from {weights_file}: {e}')
        return {}
    except Exception as e:
        bt.logging.error(f'Unexpected error loading language weights: {e}')
        return {}
