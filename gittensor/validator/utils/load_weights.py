# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt

from gittensor.constants import DEFAULT_REPO_WEIGHT, NON_CODE_EXTENSIONS

EMISSION_SHARE_SUM_TOLERANCE = 1e-9


@dataclass
class LanguageConfig:
    """Configuration for a programming language extension.

    Attributes:
        weight: Language complexity weight for scoring
        language: Tree-sitter language name (None if not supported by tree-sitter)
    """

    weight: float
    language: Optional[str] = None


@dataclass(init=False)
class RepositoryConfig:
    """Configuration for a repository in the master_repositories list.

    Attributes:
        emission_share: Repository share of the scoring emission pool
        inactive_at: ISO timestamp when repository became inactive (None if active)
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

    """

    emission_share: float
    inactive_at: Optional[str] = None
    additional_acceptable_branches: Optional[List[str]] = None
    trusted_label_pipeline: bool = False
    label_multipliers: Optional[Dict[str, float]] = None
    default_label_multiplier: float = 1.0
    fixed_base_score: Optional[float] = None
    eligibility_mode: bool = True

    def __init__(
        self,
        emission_share: Optional[float] = None,
        *,
        weight: Optional[float] = None,
        inactive_at: Optional[str] = None,
        additional_acceptable_branches: Optional[List[str]] = None,
        trusted_label_pipeline: bool = False,
        label_multipliers: Optional[Dict[str, float]] = None,
        default_label_multiplier: float = 1.0,
        fixed_base_score: Optional[float] = None,
        eligibility_mode: bool = True,
    ) -> None:
        if emission_share is None:
            emission_share = weight
        if emission_share is None:
            emission_share = DEFAULT_REPO_WEIGHT

        self.emission_share = float(emission_share)
        self.inactive_at = inactive_at
        self.additional_acceptable_branches = additional_acceptable_branches
        self.trusted_label_pipeline = trusted_label_pipeline
        self.label_multipliers = label_multipliers
        self.default_label_multiplier = default_label_multiplier
        self.fixed_base_score = fixed_base_score
        self.eligibility_mode = eligibility_mode

    @property
    def weight(self) -> float:
        """Compatibility alias while scoring consumers migrate off repo weights."""
        return self.emission_share


def resolve_repo_weight(repo_config: Optional[RepositoryConfig]) -> float:
    """Return the repo weight preserving full JSON precision, or the default for unknown repos."""
    if repo_config is None:
        return DEFAULT_REPO_WEIGHT
    return repo_config.emission_share


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


def _validate_emission_share(repo_name: str, emission_share: float) -> None:
    if not math.isfinite(emission_share):
        raise ValueError(f'{repo_name} emission_share must be finite, got {emission_share!r}')
    if not 0.0 <= emission_share <= 1.0:
        raise ValueError(f'{repo_name} emission_share must be within [0, 1], got {emission_share}')


def _validate_total_emission_share(repositories: Dict[str, RepositoryConfig]) -> None:
    total_share = sum(config.emission_share for config in repositories.values())
    if total_share < -EMISSION_SHARE_SUM_TOLERANCE or total_share > 1.0 + EMISSION_SHARE_SUM_TOLERANCE:
        raise ValueError(f'Total repository emission_share must be within [0, 1], got {total_share}')


def load_master_repo_weights() -> Dict[str, RepositoryConfig]:
    """
    Load repository emission shares from the local JSON file.
    Normalizes repository names to lowercase for case-insensitive matching.

    Returns:
        Dictionary mapping normalized (lowercase) fullName (str) to RepositoryConfig object.
        Returns empty dict when the file is missing or malformed JSON.
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
            if not isinstance(metadata, dict):
                raise ValueError(f'Expected dict metadata for {repo_name}, got {type(metadata)}')

            emission_share = float(metadata.get('emission_share', metadata.get('weight', DEFAULT_REPO_WEIGHT)))
            _validate_emission_share(repo_name, emission_share)

            try:
                config = RepositoryConfig(
                    emission_share=emission_share,
                    inactive_at=metadata.get('inactive_at'),
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
                )
                normalized_data[repo_name.lower()] = config
            except (ValueError, TypeError) as e:
                raise ValueError(f'Could not parse config for {repo_name}: {e}') from e

        _validate_total_emission_share(normalized_data)
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
