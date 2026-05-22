# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import bittensor as bt

from gittensor.constants import DEFAULT_ISSUE_DISCOVERY_SHARE, EMISSION_SHARE_TOLERANCE, NON_CODE_EXTENSIONS


@dataclass
class LanguageConfig:
    """Configuration for a programming language extension.

    Attributes:
        weight: Language complexity weight for scoring
        language: Tree-sitter language name (None if not supported by tree-sitter)
    """

    weight: float
    language: Optional[str] = None


@dataclass
class RepositoryConfig:
    """Configuration for a repository in the master_repositories list.

    Attributes:
        emission_share: Fraction of the combined scoring pool allocated to this repo
        issue_discovery_share: Fraction of the repo allocation reserved for issue discovery
        additional_acceptable_branches: List of additional branch patterns to accept (None if only default branch)
        trusted_label_pipeline: When True, scoring labels count regardless of
            actor — including GitHub Apps that surface as ``actor_association=NULL``.
            Defaults to False; only enable on repos with an authoritative label
            pipeline. See ``_resolve_trusted_scoring_label`` for the threat model.
        label_multipliers: Per-repo label pattern multipliers. Keys support the
            same fnmatch wildcard syntax as ``additional_acceptable_branches``.
        default_label_multiplier: Multiplier used when no configured label
            pattern matches. Defaults to neutral scoring.
        fixed_base_score: Override for the PR base score. Expected
            to be within [0.0, 100.0]; range is enforced by the live-config test.
        eligibility_mode: Flag controlling whether the global miner
            eligibility gate applies to PRs in this repo.
        maintainer_cut: Fraction [0.0, 1.0] of this repo's emission slice
            routed directly to its maintainer miner neurons, split evenly,
            before normal scoring. Defaults to 0.0 (no carve-out).

    """

    emission_share: float
    issue_discovery_share: float = DEFAULT_ISSUE_DISCOVERY_SHARE
    additional_acceptable_branches: Optional[List[str]] = None
    trusted_label_pipeline: bool = False
    label_multipliers: Optional[Dict[str, float]] = None
    default_label_multiplier: float = 1.0
    fixed_base_score: Optional[float] = None
    eligibility_mode: bool = True
    maintainer_cut: float = 0.0


@dataclass
class TokenConfig:
    """Configuration for token-based scoring weights.

    Attributes:
        structural_bonus: Weights for structural AST nodes (functions, classes, etc.)
        leaf_tokens: Weights for leaf AST nodes (identifiers, literals, etc.)
        language_configs: Language configurations from programming_languages.json
    """

    structural_bonus: Dict[str, float] = field(default_factory=dict)
    leaf_tokens: Dict[str, float] = field(default_factory=dict)
    language_configs: Dict[str, LanguageConfig] = field(default_factory=dict)

    def get_structural_weight(self, node_type: str) -> float:
        """Get weight for a structural node type."""
        return self.structural_bonus.get(node_type, 0.0)

    def get_leaf_weight(self, node_type: str) -> float:
        """Get weight for a leaf token type."""
        return self.leaf_tokens.get(node_type, 0.0)

    def get_language(self, extension: str) -> Optional[str]:
        """Get the tree-sitter language name for a file extension."""
        ext = extension.lstrip('.').lower()
        config = self.language_configs.get(ext)
        return config.language if config else None

    def supports_tree_sitter(self, extension: Optional[str]) -> bool:
        """Check if a file extension is supported by tree-sitter."""
        if not extension:
            return False
        ext = extension.lstrip('.').lower()
        # Non-code extensions use line-count scoring, not tree-sitter
        if ext in NON_CODE_EXTENSIONS:
            return False
        config = self.language_configs.get(ext)
        return config is not None and config.language is not None

def _get_weights_dir() -> Path:
    return Path(__file__).parent.parent / 'weights'


class RepositoryRegistryError(ValueError):
    """Raised when master_repositories.json violates the emission_share invariants."""


def _coerce_share(repo_name: str, field_name: str, raw_value: Any) -> float:
    if isinstance(raw_value, bool):
        raise RepositoryRegistryError(f'{repo_name} {field_name} must be a float, got bool')
    return float(raw_value)


def _validate_emission_shares(configs: Dict[str, RepositoryConfig]) -> None:
    total_share = 0.0
    for repo_name, config in configs.items():
        if not 0.0 <= config.emission_share <= 1.0:
            raise RepositoryRegistryError(
                f'{repo_name} emission_share must be within [0, 1], got {config.emission_share}'
            )
        if not 0.0 <= config.issue_discovery_share <= 1.0:
            raise RepositoryRegistryError(
                f'{repo_name} issue_discovery_share must be within [0, 1], got {config.issue_discovery_share}'
            )
        if not 0.0 <= config.maintainer_cut <= 1.0:
            raise RepositoryRegistryError(
                f'{repo_name} maintainer_cut must be within [0, 1], got {config.maintainer_cut}'
            )
        total_share += config.emission_share

    if total_share > 1.0 + EMISSION_SHARE_TOLERANCE:
        raise RepositoryRegistryError(f'total emission_share must be <= 1.0, got {total_share}')


def load_master_repo_weights() -> Dict[str, RepositoryConfig]:
    """
    Load repository emission shares from the local JSON file.
    Normalizes repository names to lowercase for case-insensitive matching.

    Returns:
        Dictionary mapping normalized (lowercase) fullName (str) to RepositoryConfig object.
        Returns empty dict when the file is missing or invalid JSON. Raises
        RepositoryRegistryError or ValueError when registry entries violate the
        emission-share contract.
    """
    weights_file = _get_weights_dir() / 'master_repositories.json'

    try:
        with open(weights_file, 'r') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise RepositoryRegistryError(f'Expected dict from {weights_file}, got {type(data)}')

        # Parse JSON data into RepositoryConfig objects
        normalized_data: Dict[str, RepositoryConfig] = {}
        for repo_name, metadata in data.items():
            try:
                if not isinstance(metadata, dict):
                    raise TypeError(f'expected object metadata, got {type(metadata)}')
                config = RepositoryConfig(
                    emission_share=_coerce_share(repo_name, 'emission_share', metadata['emission_share']),
                    issue_discovery_share=_coerce_share(
                        repo_name,
                        'issue_discovery_share',
                        metadata.get('issue_discovery_share', DEFAULT_ISSUE_DISCOVERY_SHARE),
                    ),
                    additional_acceptable_branches=metadata.get('additional_acceptable_branches'),
                    trusted_label_pipeline=bool(metadata.get('trusted_label_pipeline', False)),
                    label_multipliers=(
                        {str(label): float(multiplier) for label, multiplier in metadata['label_multipliers'].items()}
                        if metadata.get('label_multipliers') is not None
                        else None
                    ),
                    default_label_multiplier=float(metadata.get('default_label_multiplier', 1.0)),
                    fixed_base_score=metadata.get('fixed_base_score'),
                    eligibility_mode=metadata.get('eligibility_mode', True),
                    maintainer_cut=_coerce_share(repo_name, 'maintainer_cut', metadata.get('maintainer_cut', 0.0)),
                )
                normalized_data[repo_name.lower()] = config
            except RepositoryRegistryError:
                raise
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f'Could not parse config for {repo_name}: {e}') from e

        _validate_emission_shares(normalized_data)

        bt.logging.debug(f'Successfully loaded {len(normalized_data)} repository entries from {weights_file}')
        return normalized_data

    except FileNotFoundError:
        bt.logging.error(f'Weights file not found: {weights_file}')
        return {}
    except json.JSONDecodeError as e:
        bt.logging.error(f'Failed to parse JSON from {weights_file}: {e}')
        return {}


def load_programming_language_weights() -> Dict[str, LanguageConfig]:
    """
    Load programming language weights from the local JSON file.

    Returns:
        Dictionary mapping extension (str) to LanguageConfig object.
        Returns empty dict on error.
    """
    weights_file = _get_weights_dir() / 'programming_languages.json'

    try:
        with open(weights_file, 'r') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            bt.logging.error(f'Expected dict from {weights_file}, got {type(data)}')
            return {}

        result: Dict[str, LanguageConfig] = {}
        for extension, config in data.items():
            try:
                if isinstance(config, dict):
                    result[extension] = LanguageConfig(
                        weight=float(config.get('weight', 1.0)),
                        language=config.get('language'),
                    )
                else:
                    # Backwards compatibility: handle plain float values
                    result[extension] = LanguageConfig(weight=float(config))
            except (ValueError, TypeError) as e:
                bt.logging.warning(f'Could not parse config for {extension}: {config} - {e}')
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


def load_token_config() -> TokenConfig:
    """Load token configuration from JSON files.

    Loads structural and leaf token weights from token_weights.json,
    and language configurations from programming_languages.json.

    Returns:
        TokenConfig with all scoring configuration loaded.
    """
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

    # Load language configurations (includes tree-sitter language mapping)
    language_configs = load_programming_language_weights()

    # Count languages with tree-sitter support
    tree_sitter_count = sum(1 for c in language_configs.values() if c.language is not None)

    config = TokenConfig(
        structural_bonus=structural_bonus,
        leaf_tokens=leaf_tokens,
        language_configs=language_configs,
    )

    bt.logging.info(
        f'Loaded token config: {len(structural_bonus)} structural, '
        f'{len(leaf_tokens)} leaf, {tree_sitter_count} tree-sitter languages'
    )

    return config
