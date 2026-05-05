# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt

from gittensor.constants import DEFAULT_REPO_WEIGHT, NON_CODE_EXTENSIONS

_LABEL_MULTIPLIER_MIN = 0.0
_LABEL_MULTIPLIER_MAX = 20.0
_LABEL_MULTIPLIERS_MAX_ENTRIES = 10


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
        weight: Repository weight for scoring
        inactive_at: ISO timestamp when repository became inactive (None if active)
        additional_acceptable_branches: List of additional branch patterns to accept (None if only default branch)
        mirror_enabled: When True, fetch this repo's data from the das-github-mirror
            service instead of via per-miner PATs. Defaults to False so existing
            entries keep their current PAT-based behavior.
        trusted_label_pipeline: When True, scoring labels count regardless of
            actor — including GitHub Apps that surface as ``actor_association=NULL``.
            Defaults to False; only enable on repos with an authoritative label
            pipeline. See ``_resolve_trusted_scoring_label`` for the threat model.
        label_multipliers: Per-repo label-to-multiplier mapping. Keys support
            fnmatch wildcards (e.g. ``"type/*"``, ``"*-dev"``). Each value must
            be in [0.0, 20.0]; at most 10 entries. When None no label scoring
            applies and ``default_label_multiplier`` is used for all PRs.
        default_label_multiplier: Multiplier applied when no label on the PR
            matches any pattern in ``label_multipliers``. Must be in [0.0, 20.0].
            Defaults to 1.0 (neutral).
    """

    weight: float
    inactive_at: Optional[str] = None
    additional_acceptable_branches: Optional[List[str]] = None
    mirror_enabled: bool = False
    trusted_label_pipeline: bool = False
    label_multipliers: Optional[Dict[str, float]] = None
    default_label_multiplier: float = 1.0


def resolve_repo_weight(repo_config: Optional[RepositoryConfig]) -> float:
    """Return the repo weight preserving full JSON precision, or the default for unknown repos."""
    if repo_config is None:
        return DEFAULT_REPO_WEIGHT
    return repo_config.weight


def resolve_label_multiplier(label: str, repo_config: Optional[RepositoryConfig]) -> Optional[float]:
    """Return the per-repo multiplier for *label* using fnmatch pattern matching.

    Patterns and label are compared in lowercase. Returns ``None`` when no
    pattern matches — callers should then fall back to
    ``repo_config.default_label_multiplier`` (or 1.0 if config is absent).
    """
    if repo_config is None or not repo_config.label_multipliers:
        return None
    label_lower = label.lower()
    for pattern, multiplier in repo_config.label_multipliers.items():
        if fnmatch(label_lower, pattern.lower()):
            return multiplier
    return None


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
                raw_lm = metadata.get('label_multipliers')
                label_multipliers: Optional[Dict[str, float]] = None
                if raw_lm is not None:
                    if not isinstance(raw_lm, dict):
                        bt.logging.warning(f'{repo_name}: label_multipliers must be a dict, ignoring')
                    elif len(raw_lm) > _LABEL_MULTIPLIERS_MAX_ENTRIES:
                        bt.logging.warning(
                            f'{repo_name}: label_multipliers has {len(raw_lm)} entries '
                            f'(max {_LABEL_MULTIPLIERS_MAX_ENTRIES}), ignoring'
                        )
                    else:
                        validated: Dict[str, float] = {}
                        for k, v in raw_lm.items():
                            fv = float(v)
                            if not (_LABEL_MULTIPLIER_MIN <= fv <= _LABEL_MULTIPLIER_MAX):
                                bt.logging.warning(
                                    f'{repo_name}: label_multipliers["{k}"]={fv} out of '
                                    f'[{_LABEL_MULTIPLIER_MIN}, {_LABEL_MULTIPLIER_MAX}], skipping entry'
                                )
                            else:
                                validated[k] = fv
                        label_multipliers = validated or None

                raw_default = metadata.get('default_label_multiplier', 1.0)
                default_label_multiplier = float(raw_default)
                if not (_LABEL_MULTIPLIER_MIN <= default_label_multiplier <= _LABEL_MULTIPLIER_MAX):
                    bt.logging.warning(
                        f'{repo_name}: default_label_multiplier={default_label_multiplier} out of '
                        f'[{_LABEL_MULTIPLIER_MIN}, {_LABEL_MULTIPLIER_MAX}], resetting to 1.0'
                    )
                    default_label_multiplier = 1.0

                config = RepositoryConfig(
                    weight=float(metadata.get('weight', 0.01)),
                    inactive_at=metadata.get('inactive_at'),
                    additional_acceptable_branches=metadata.get('additional_acceptable_branches'),
                    mirror_enabled=bool(metadata.get('mirror_enabled', False)),
                    trusted_label_pipeline=bool(metadata.get('trusted_label_pipeline', False)),
                    label_multipliers=label_multipliers,
                    default_label_multiplier=default_label_multiplier,
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
