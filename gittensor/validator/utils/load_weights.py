# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import bittensor as bt

from gittensor.constants import NON_CODE_EXTENSIONS


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

    """

    weight: float
    inactive_at: Optional[str] = None
    additional_acceptable_branches: Optional[List[str]] = None


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


_T = TypeVar('_T')


def _load_json_file(filename: str, label: str) -> Any:
    """Open a JSON file from the weights directory and return its parsed content.

    Logs an error and re-raises on ``FileNotFoundError`` or ``json.JSONDecodeError``.
    """
    weights_file = _get_weights_dir() / filename
    try:
        with open(weights_file, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        bt.logging.error(f'{label.title()} weights file not found: {weights_file}')
        raise
    except json.JSONDecodeError as e:
        bt.logging.error(f'Invalid JSON in {label} weights file {weights_file}: {e}')
        raise


def _load_json_weights(
    filename: str,
    parse_entry: Callable[[str, Any], Tuple[str, _T]],
    label: str,
) -> Dict[str, _T]:
    """Load a JSON weights dict and parse each entry via *parse_entry*.

    ``parse_entry(key, value)`` returns ``(normalised_key, parsed_object)``.
    If it raises ``ValueError`` or ``TypeError`` the entry is logged and skipped.

    Returns empty dict on any file-level or structural error.
    """
    try:
        data = _load_json_file(filename, label)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        bt.logging.error(f'Unexpected error loading {label} weights: {e}')
        return {}

    if not isinstance(data, dict):
        bt.logging.error(f'Expected dict from {_get_weights_dir() / filename}, got {type(data)}')
        return {}

    result: Dict[str, _T] = {}
    for key, value in data.items():
        try:
            norm_key, parsed = parse_entry(key, value)
            result[norm_key] = parsed
        except (ValueError, TypeError) as e:
            bt.logging.warning(f'Could not parse {label} config for {key}: {e}')

    bt.logging.debug(f'Successfully loaded {len(result)} {label} entries from {_get_weights_dir() / filename}')
    return result


def load_master_repo_weights() -> Dict[str, RepositoryConfig]:
    """Load repository weights from the local JSON file.

    Normalizes repository names to lowercase for case-insensitive matching.

    Returns:
        Dictionary mapping normalized (lowercase) fullName to RepositoryConfig.
        Returns empty dict on error.
    """

    def _parse_repo(name: str, metadata: Any) -> Tuple[str, RepositoryConfig]:
        try:
            config = RepositoryConfig(
                weight=float(metadata.get('weight', 0.01)),
                inactive_at=metadata.get('inactive_at'),
                additional_acceptable_branches=metadata.get('additional_acceptable_branches'),
            )
        except (ValueError, TypeError) as e:
            bt.logging.warning(f'Could not parse config for {name}: {e}, using defaults')
            config = RepositoryConfig(weight=float(metadata.get('weight', 0.01)))
        return name.lower(), config

    return _load_json_weights('master_repositories.json', _parse_repo, 'repository')


def load_programming_language_weights() -> Dict[str, LanguageConfig]:
    """Load programming language weights from the local JSON file.

    Returns:
        Dictionary mapping extension to LanguageConfig.
        Returns empty dict on error.
    """

    def _parse_lang(extension: str, config: Any) -> Tuple[str, LanguageConfig]:
        if isinstance(config, dict):
            return extension, LanguageConfig(
                weight=float(config.get('weight', 1.0)),
                language=config.get('language'),
            )
        return extension, LanguageConfig(weight=float(config))

    return _load_json_weights('programming_languages.json', _parse_lang, 'language')


def load_token_config() -> TokenConfig:
    """Load token configuration from JSON files.

    Loads structural and leaf token weights from token_weights.json,
    and language configurations from programming_languages.json.

    Returns:
        TokenConfig with all scoring configuration loaded.

    Raises:
        FileNotFoundError: If token_weights.json is missing.
        json.JSONDecodeError: If token_weights.json contains invalid JSON.
    """
    data = _load_json_file('token_weights.json', 'token')

    structural_bonus = dict(data.get('structural_bonus', {}))
    leaf_tokens = dict(data.get('leaf_tokens', {}))
    language_configs = load_programming_language_weights()
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
