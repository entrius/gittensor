# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt

from gittensor.constants import NON_CODE_EXTENSIONS

REGISTRY_EMISSION_SUM_TOLERANCE = 1e-9


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
        emission_share: This repo's share of the unified OSS scoring pool for the
            round, in [0, 1]. Values across the registry must sum to <= 1.0.
        issue_discovery_share: Fraction of this repo's slice allocated to issue
            discovery scoring before same-repo spill; [0, 1]. Defaults to 0.5
            when omitted from JSON.
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
    issue_discovery_share: float = 0.5
    inactive_at: Optional[str] = None
    additional_acceptable_branches: Optional[List[str]] = None
    trusted_label_pipeline: bool = False
    label_multipliers: Optional[Dict[str, float]] = None
    default_label_multiplier: float = 1.0
    fixed_base_score: Optional[float] = None
    eligibility_mode: bool = True


def validate_registry_emission_shares(configs: Dict[str, RepositoryConfig]) -> None:
    """Raise ``ValueError`` if any entry is out of range or shares sum above 1.0."""
    total = sum(c.emission_share for c in configs.values())
    if total > 1.0 + REGISTRY_EMISSION_SUM_TOLERANCE:
        raise ValueError(f'master_repositories.json: emission_share values sum to {total}, must be <= 1.0')
    for name, cfg in configs.items():
        if cfg.emission_share < -1e-12 or cfg.emission_share > 1.0 + 1e-12:
            raise ValueError(f'{name}: emission_share {cfg.emission_share} out of [0, 1]')
        if cfg.issue_discovery_share < -1e-12 or cfg.issue_discovery_share > 1.0 + 1e-12:
            raise ValueError(f'{name}: issue_discovery_share {cfg.issue_discovery_share} out of [0, 1]')


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


def _parse_repo_metadata(repo_name: str, metadata: dict) -> RepositoryConfig:
    share_raw = metadata.get('emission_share', metadata.get('weight'))
    if share_raw is None:
        emission_share = 0.01
    else:
        emission_share = float(share_raw)
    ids_raw = metadata.get('issue_discovery_share')
    issue_discovery_share = 0.5 if ids_raw is None else float(ids_raw)
    return RepositoryConfig(
        emission_share=emission_share,
        issue_discovery_share=issue_discovery_share,
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


def load_master_repo_weights() -> Dict[str, RepositoryConfig]:
    """
    Load repository config from the local JSON file.
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

        normalized_data: Dict[str, RepositoryConfig] = {}
        for repo_name, metadata in data.items():
            if not isinstance(metadata, dict):
                bt.logging.warning(f'Could not parse config for {repo_name}: expected object')
                continue
            try:
                config = _parse_repo_metadata(repo_name, metadata)
                normalized_data[repo_name.lower()] = config
            except (ValueError, TypeError) as e:
                raise ValueError(f'master_repositories.json: invalid entry {repo_name!r}: {e}') from e

        validate_registry_emission_shares(normalized_data)

        bt.logging.debug(f'Successfully loaded {len(normalized_data)} repository entries from {weights_file}')
        return normalized_data

    except FileNotFoundError:
        bt.logging.error(f'Weights file not found: {weights_file}')
        return {}
    except json.JSONDecodeError as e:
        bt.logging.error(f'Failed to parse JSON from {weights_file}: {e}')
        return {}
    except ValueError as e:
        bt.logging.error(f'Invalid repository registry: {e}')
        raise
    except Exception as e:
        bt.logging.error(f'Unexpected error loading repository weights: {e}')
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
