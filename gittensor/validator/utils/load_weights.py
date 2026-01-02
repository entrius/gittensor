# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import bittensor as bt

from gittensor.constants import MITIGATED_EXTENSIONS
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


@dataclass
class TokenWeights:
    """Configuration for token-based scoring weights."""

    structural_bonus: Dict[str, float] = field(default_factory=dict)
    leaf_tokens: Dict[str, float] = field(default_factory=dict)
    extension_to_language: Dict[str, str] = field(default_factory=dict)
    documentation_extensions: Set[str] = field(default_factory=set)

    def get_structural_weight(self, node_type: str) -> float:
        return self.structural_bonus.get(node_type, 0.0)

    def get_leaf_weight(self, node_type: str) -> float:
        return self.leaf_tokens.get(node_type, 0.0)

    def get_language(self, extension: str) -> Optional[str]:
        """Get the tree-sitter language name for a file extension."""
        ext = extension.lstrip('.').lower()
        return self.extension_to_language.get(ext)

    def is_documentation_file(self, extension: str) -> bool:
        ext = extension.lstrip('.').lower()
        return ext in self.documentation_extensions

    def supports_tree_sitter(self, extension: str) -> bool:
        """Check if a file extension is supported by tree-sitter."""
        ext = extension.lstrip('.').lower()
        return ext in self.extension_to_language and ext not in self.documentation_extensions


def _get_weights_dir() -> Path:
    return Path(__file__).parent.parent / 'weights'


def load_master_repo_weights() -> Dict[str, RepositoryConfig]:
    """
    Load repository weights from the local JSON file.
    Normalizes repository names to lowercase for case-insensitive matching.

    Returns:
        Dictionary mapping normalized (lowercase) fullName (str) to RepositoryConfig object.
        Returns empty dict on error.
    """
    weights_file = _get_weights_dir() / 'master_repositories.json'

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
    weights_file = _get_weights_dir() / 'programming_languages.json'

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


def load_extension_to_language() -> Dict[str, str]:
    """Load extension to tree-sitter language mapping."""
    path = _get_weights_dir() / 'extension_to_language.json'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        bt.logging.error(f'Failed to load extension_to_language.json: {e}')
        return {}


def load_token_weights() -> TokenWeights:
    """Load token weights from JSON configuration files."""
    weights_file = _get_weights_dir() / 'token_weights.json'

    try:
        with open(weights_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        bt.logging.error(f'Token weights file not found: {weights_file}')
        raise
    except json.JSONDecodeError as e:
        bt.logging.error(f'Invalid JSON in token weights file: {e}')
        raise

    structural_bonus = dict(data.get('structural_bonus', {}))
    leaf_tokens = dict(data.get('leaf_tokens', {}))
    extension_to_language = load_extension_to_language()

    weights = TokenWeights(
        structural_bonus=structural_bonus,
        leaf_tokens=leaf_tokens,
        extension_to_language=extension_to_language,
        documentation_extensions=set(MITIGATED_EXTENSIONS),
    )

    bt.logging.info(
        f'Loaded token weights: {len(structural_bonus)} structural, '
        f'{len(leaf_tokens)} leaf, {len(extension_to_language)} languages'
    )

    return weights


def get_supported_extensions() -> List[str]:
    """
    Get a list of file extensions supported by tree-sitter scoring.

    Returns:
        List[str]: List of supported file extensions (without dots).
    """
    weights = load_token_weights()
    return [ext for ext in weights.extension_to_language.keys() if ext not in weights.documentation_extensions]


def get_documentation_extensions() -> List[str]:
    """
    Get a list of file extensions that use regex-based scoring.

    Returns:
        List[str]: List of documentation file extensions (without dots).
    """
    weights = load_token_weights()
    return list(weights.documentation_extensions)
