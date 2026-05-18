# The MIT License (MIT)
# Copyright © 2025 Entrius
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import bittensor as bt

from gittensor.constants import (
    DEFAULT_ISSUE_DISCOVERY_SHARE,
    EMISSION_SHARE_TOLERANCE,
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_ISSUE_THRESHOLD,
    MAX_OPEN_PR_THRESHOLD,
    MIN_CREDIBILITY,
    MIN_ISSUE_CREDIBILITY,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    MIN_TOKEN_SCORE_FOR_VALID_ISSUE,
    MIN_VALID_MERGED_PRS,
    MIN_VALID_SOLVED_ISSUES,
    NON_CODE_EXTENSIONS,
    OPEN_ISSUE_SPAM_BASE_THRESHOLD,
    OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT,
    OPEN_PR_THRESHOLD_TOKEN_SCORE,
)


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
class RepoEligibilityConfig:
    """Per-repo overrides for the eligibility / spam knobs.

    Every field is optional; ``None`` means "use the global default constant".
    Resolve a config into concrete values with ``resolve_eligibility``.
    """

    min_valid_merged_prs: Optional[int] = None
    min_credibility: Optional[float] = None
    min_token_score_for_base_score: Optional[float] = None
    excessive_pr_penalty_base_threshold: Optional[int] = None
    open_pr_threshold_token_score: Optional[float] = None
    max_open_pr_threshold: Optional[int] = None
    min_valid_solved_issues: Optional[int] = None
    min_issue_credibility: Optional[float] = None
    min_token_score_for_valid_issue: Optional[float] = None
    open_issue_spam_base_threshold: Optional[int] = None
    open_issue_spam_token_score_per_slot: Optional[float] = None
    max_open_issue_threshold: Optional[int] = None


@dataclass(frozen=True)
class ResolvedEligibility:
    """A ``RepoEligibilityConfig`` with every override resolved to a concrete value."""

    min_valid_merged_prs: int
    min_credibility: float
    min_token_score_for_base_score: float
    excessive_pr_penalty_base_threshold: int
    open_pr_threshold_token_score: float
    max_open_pr_threshold: int
    min_valid_solved_issues: int
    min_issue_credibility: float
    min_token_score_for_valid_issue: float
    open_issue_spam_base_threshold: int
    open_issue_spam_token_score_per_slot: float
    max_open_issue_threshold: int


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
        eligibility: Per-repo overrides for the eligibility / spam knobs. Unset
            fields fall back to the global default constants — see
            ``resolve_eligibility``.
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
    eligibility: RepoEligibilityConfig = field(default_factory=RepoEligibilityConfig)
    maintainer_cut: float = 0.0


def resolve_eligibility(cfg: Optional[RepoEligibilityConfig]) -> ResolvedEligibility:
    """Overlay a repo's eligibility overrides onto the global default constants."""
    cfg = cfg or RepoEligibilityConfig()

    def pick(value: Any, default: Any) -> Any:
        return default if value is None else value

    return ResolvedEligibility(
        min_valid_merged_prs=int(pick(cfg.min_valid_merged_prs, MIN_VALID_MERGED_PRS)),
        min_credibility=float(pick(cfg.min_credibility, MIN_CREDIBILITY)),
        min_token_score_for_base_score=float(pick(cfg.min_token_score_for_base_score, MIN_TOKEN_SCORE_FOR_BASE_SCORE)),
        excessive_pr_penalty_base_threshold=int(
            pick(cfg.excessive_pr_penalty_base_threshold, EXCESSIVE_PR_PENALTY_BASE_THRESHOLD)
        ),
        open_pr_threshold_token_score=float(pick(cfg.open_pr_threshold_token_score, OPEN_PR_THRESHOLD_TOKEN_SCORE)),
        max_open_pr_threshold=int(pick(cfg.max_open_pr_threshold, MAX_OPEN_PR_THRESHOLD)),
        min_valid_solved_issues=int(pick(cfg.min_valid_solved_issues, MIN_VALID_SOLVED_ISSUES)),
        min_issue_credibility=float(pick(cfg.min_issue_credibility, MIN_ISSUE_CREDIBILITY)),
        min_token_score_for_valid_issue=float(
            pick(cfg.min_token_score_for_valid_issue, MIN_TOKEN_SCORE_FOR_VALID_ISSUE)
        ),
        open_issue_spam_base_threshold=int(pick(cfg.open_issue_spam_base_threshold, OPEN_ISSUE_SPAM_BASE_THRESHOLD)),
        open_issue_spam_token_score_per_slot=float(
            pick(cfg.open_issue_spam_token_score_per_slot, OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT)
        ),
        max_open_issue_threshold=int(pick(cfg.max_open_issue_threshold, MAX_OPEN_ISSUE_THRESHOLD)),
    )


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


_ELIGIBILITY_INT_FIELDS = (
    'min_valid_merged_prs',
    'excessive_pr_penalty_base_threshold',
    'max_open_pr_threshold',
    'min_valid_solved_issues',
    'open_issue_spam_base_threshold',
    'max_open_issue_threshold',
)
_ELIGIBILITY_FLOAT_FIELDS = (
    'min_credibility',
    'min_token_score_for_base_score',
    'open_pr_threshold_token_score',
    'min_issue_credibility',
    'min_token_score_for_valid_issue',
    'open_issue_spam_token_score_per_slot',
)


def _coerce_eligibility_value(repo_name: str, field_name: str, raw_value: Any, caster: Any) -> Any:
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        raise RepositoryRegistryError(f'{repo_name} eligibility.{field_name} must be a number, got bool')
    try:
        return caster(raw_value)
    except (TypeError, ValueError) as e:
        raise RepositoryRegistryError(f'{repo_name} eligibility.{field_name} must be a number: {e}') from e


def _parse_eligibility(repo_name: str, raw: Any) -> RepoEligibilityConfig:
    """Parse the optional ``eligibility`` object from a master_repositories.json entry."""
    if raw is None:
        return RepoEligibilityConfig()
    if not isinstance(raw, dict):
        raise RepositoryRegistryError(f'{repo_name} eligibility must be an object, got {type(raw)}')

    known = set(_ELIGIBILITY_INT_FIELDS) | set(_ELIGIBILITY_FLOAT_FIELDS)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise RepositoryRegistryError(f'{repo_name} eligibility has unknown keys: {unknown}')

    kwargs: Dict[str, Any] = {}
    for field_name in _ELIGIBILITY_INT_FIELDS:
        kwargs[field_name] = _coerce_eligibility_value(repo_name, field_name, raw.get(field_name), int)
    for field_name in _ELIGIBILITY_FLOAT_FIELDS:
        kwargs[field_name] = _coerce_eligibility_value(repo_name, field_name, raw.get(field_name), float)
    return RepoEligibilityConfig(**kwargs)


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


def _validate_eligibility_configs(configs: Dict[str, RepositoryConfig]) -> None:
    """Range-check every repo's resolved eligibility config."""
    for repo_name, config in configs.items():
        resolved = resolve_eligibility(config.eligibility)
        for field_name in ('min_credibility', 'min_issue_credibility'):
            value = getattr(resolved, field_name)
            if not 0.0 <= value <= 1.0:
                raise RepositoryRegistryError(
                    f'{repo_name} eligibility.{field_name} must be within [0, 1], got {value}'
                )
        non_negative = (
            'min_valid_merged_prs',
            'min_token_score_for_base_score',
            'excessive_pr_penalty_base_threshold',
            'max_open_pr_threshold',
            'min_valid_solved_issues',
            'min_token_score_for_valid_issue',
            'open_issue_spam_base_threshold',
            'max_open_issue_threshold',
        )
        for field_name in non_negative:
            value = getattr(resolved, field_name)
            if value < 0:
                raise RepositoryRegistryError(f'{repo_name} eligibility.{field_name} must be >= 0, got {value}')
        # Used as divisors in the open-PR / open-issue slot math.
        for field_name in ('open_pr_threshold_token_score', 'open_issue_spam_token_score_per_slot'):
            value = getattr(resolved, field_name)
            if value <= 0:
                raise RepositoryRegistryError(
                    f'{repo_name} eligibility.{field_name} must be > 0 (used as a divisor), got {value}'
                )


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
                    eligibility=_parse_eligibility(repo_name, metadata.get('eligibility')),
                    maintainer_cut=_coerce_share(repo_name, 'maintainer_cut', metadata.get('maintainer_cut', 0.0)),
                )
                normalized_data[repo_name.lower()] = config
            except RepositoryRegistryError:
                raise
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f'Could not parse config for {repo_name}: {e}') from e

        _validate_emission_shares(normalized_data)
        _validate_eligibility_configs(normalized_data)

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
