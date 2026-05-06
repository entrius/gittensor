# The MIT License (MIT)
# Copyright © 2025 Entrius
import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt

from gittensor.constants import DEFAULT_REPO_WEIGHT, NON_CODE_EXTENSIONS

# Default when ``default_label_multiplier`` is omitted from master_repositories.json
DEFAULT_LABEL_MULTIPLIER = 1.0


def max_multiplier_for_label(label_name: str, label_multipliers: Dict[str, float]) -> Optional[float]:
    """Return the highest multiplier among ``label_multipliers`` keys that match ``label_name``.

    Keys use the same ``fnmatch`` wildcard semantics as ``additional_acceptable_branches``
    (see ``branch_matches_pattern``). ``label_name`` is matched case-insensitively.
    """
    label_lower = label_name.lower()
    best: Optional[float] = None
    for pattern, mult in label_multipliers.items():
        if fnmatch.fnmatch(label_lower, (pattern or '').lower()):
            if best is None or mult > best:
                best = mult
    return best


def resolve_label_multiplier(repo_config: Optional['RepositoryConfig'], chosen_label: Optional[str]) -> float:
    """Label multiplier for a PR given the repo config and the resolved scoring label (if any).

    When ``label_multipliers`` is empty or missing, every PR uses ``default_label_multiplier``.
    When labels exist but none match a configured pattern, the resolved label is ``None`` and
    this returns ``default_label_multiplier`` as well.
    """
    default_m = repo_config.default_label_multiplier if repo_config else DEFAULT_LABEL_MULTIPLIER
    if not repo_config or not chosen_label:
        return default_m
    lm = repo_config.label_multipliers
    if not lm:
        return default_m
    matched = max_multiplier_for_label(chosen_label, lm)
    return matched if matched is not None else default_m


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
        label_multipliers: Optional mapping of wildcard label patterns to multipliers.
            When omitted or empty, PR labels do not affect scoring (every PR uses
            ``default_label_multiplier``). Pattern syntax matches
            ``additional_acceptable_branches`` (``fnmatch``).
        default_label_multiplier: Multiplier applied when no configured pattern
            matches, or when the PR has no scoring label. Defaults to 1.0.

    """

    weight: float
    inactive_at: Optional[str] = None
    additional_acceptable_branches: Optional[List[str]] = None
    mirror_enabled: bool = False
    trusted_label_pipeline: bool = False
    label_multipliers: Optional[Dict[str, float]] = None
    default_label_multiplier: float = DEFAULT_LABEL_MULTIPLIER


def resolve_repo_weight(repo_config: Optional[RepositoryConfig]) -> float:
    """Return the repo weight preserving full JSON precision, or the default for unknown repos."""
    if repo_config is None:
        return DEFAULT_REPO_WEIGHT
    return repo_config.weight


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
                if isinstance(raw_lm, dict) and raw_lm:
                    label_multipliers = {str(k): float(v) for k, v in raw_lm.items()}

                dlm_raw = metadata.get('default_label_multiplier')
                default_label_multiplier = float(dlm_raw) if dlm_raw is not None else DEFAULT_LABEL_MULTIPLIER

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
