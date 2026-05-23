"""Repository-scoped PR label multiplier resolution."""

from fnmatch import fnmatch
from typing import Iterable, Optional, Protocol

from gittensor.constants import MAINTAINER_ASSOCIATIONS
from gittensor.validator.utils.load_weights import RepositoryConfig


class LabelWithActor(Protocol):
    name: str
    actor_association: Optional[str]


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


def resolve_trusted_label_multiplier(
    labels: Iterable[LabelWithActor],
    repo_config: RepositoryConfig,
) -> tuple[Optional[str], float]:
    """Resolve the highest-multiplier trusted label for repository scoring.

    By default the actor must be in ``MAINTAINER_ASSOCIATIONS``. Repos opted into
    ``trusted_label_pipeline`` accept any actor, including GitHub-App actors that
    surface as ``actor_association=NULL`` because they lack a row in
    ``contributor_repo_roles`` (issue #911).
    """
    trusted = repo_config.trusted_label_pipeline
    candidate_names = [
        (label.name or '').lower()
        for label in labels
        if label.name and (trusted or label.actor_association in MAINTAINER_ASSOCIATIONS)
    ]
    return resolve_highest_label_multiplier(candidate_names, repo_config)
