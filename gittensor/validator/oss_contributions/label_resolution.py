"""Repository-scoped PR label multiplier resolution."""

from fnmatch import fnmatch
from typing import Iterable, Optional

from gittensor.validator.utils.load_weights import RepositoryConfig


def get_default_label_multiplier(repo_config: Optional[RepositoryConfig]) -> float:
    """Return the neutral/default label multiplier for a repository."""
    return repo_config.default_label_multiplier if repo_config else 1.0


def get_label_multiplier(label: str, repo_config: Optional[RepositoryConfig]) -> Optional[float]:
    """Return the highest configured multiplier matching a label, or None."""
    if repo_config is None or not repo_config.label_multipliers:
        return None

    label_lower = label.lower()
    matches = [
        multiplier
        for pattern, multiplier in repo_config.label_multipliers.items()
        if fnmatch(label_lower, pattern.lower())
    ]
    return max(matches) if matches else None


def resolve_legacy_label_multiplier(
    label_timeline_order: Iterable[str],
    current_labels: Iterable[str],
    repo_config: Optional[RepositoryConfig],
) -> tuple[Optional[str], float]:
    """Resolve the legacy PR label and multiplier from repo-specific config.

    Timeline labels are ordered newest first, preserving the legacy "last applied
    scoring label wins" behavior. If the relevant timeline event was truncated,
    the fallback mirrors the current-label behavior by choosing the highest
    multiplier, then the label name for deterministic ties.
    """
    for label in label_timeline_order:
        multiplier = get_label_multiplier(label, repo_config)
        if multiplier is not None:
            return label, multiplier

    return resolve_highest_label_multiplier(current_labels, repo_config)


def resolve_highest_label_multiplier(
    labels: Iterable[str],
    repo_config: Optional[RepositoryConfig],
) -> tuple[Optional[str], float]:
    """Resolve the highest-multiplier label from unordered candidate labels."""
    default_multiplier = get_default_label_multiplier(repo_config)
    candidates = []
    for label in labels:
        multiplier = get_label_multiplier(label, repo_config)
        if multiplier is not None:
            candidates.append((label, multiplier))

    if not candidates:
        return None, default_multiplier

    label, multiplier = max(candidates, key=lambda candidate: (candidate[1], candidate[0]))
    return label, multiplier
