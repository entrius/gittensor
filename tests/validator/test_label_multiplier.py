# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Label scoring: per-repo ``label_multipliers`` + GraphQL label extraction."""

import pytest

from gittensor.classes import PullRequest
from gittensor.validator.utils.load_weights import (
    RepositoryConfig,
    max_multiplier_for_label,
    resolve_label_multiplier,
)

# Mirrors the old global table closely enough for timeline / tie-break regression tests.
_LEGACY_LABEL_MULTIPLIERS = {
    'feature': 1.50,
    'feat': 1.50,
    'bug': 1.25,
    'fix': 1.25,
    'crash': 1.25,
    'regression': 1.25,
    'security': 1.25,
    'enhancement': 1.10,
    'improve': 1.10,
    'perf': 1.10,
    'refactor': 0.5,
    'cleanup': 0.5,
    'polish': 0.5,
    'debt': 0.5,
    'chore': 0.5,
}

_REPO_WITH_LEGACY_LABELS = RepositoryConfig(weight=0.01, label_multipliers=dict(_LEGACY_LABEL_MULTIPLIERS))


def _pr_payload(current, timeline):
    """Minimal GraphQL payload; current=list[str], timeline=list[str or None]."""
    return {
        'number': 1,
        'repository': {'owner': {'login': 'x'}, 'name': 'y'},
        'state': 'MERGED',
        'title': 't',
        'bodyText': '',
        'author': {'login': 'a'},
        'createdAt': '2026-04-20T00:00:00Z',
        'mergedAt': '2026-04-20T01:00:00Z',
        'lastEditedAt': None,
        'additions': 1,
        'deletions': 0,
        'commits': {'totalCount': 1},
        'mergedBy': {'login': 'a'},
        'headRefOid': 'h',
        'baseRefOid': 'b',
        'closingIssuesReferences': {'nodes': []},
        'changesRequestedReviews': {'nodes': []},
        'labels': {'nodes': [{'name': n} for n in current]},
        'timelineItems': {'nodes': [{'label': {'name': n} if n else None} for n in timeline]},
    }


def _parse(current, timeline, repo_config=None):
    cfg = repo_config if repo_config is not None else _REPO_WITH_LEGACY_LABELS
    return PullRequest.from_graphql_response(
        _pr_payload(current, timeline), uid=0, hotkey='hk', github_id='gh', repo_config=cfg
    ).label


@pytest.mark.parametrize(
    'current,timeline,expected',
    [
        (['feature'], ['feature'], 'feature'),
        (['size:M', 'refactor', 'lgtm'], ['size:M', 'refactor', 'lgtm'], 'refactor'),
        (['bug', 'topic:editor', 'topic:shaders'], ['bug', 'topic:editor', 'topic:shaders'], 'bug'),
        (['feature', 'bug'], ['feature', 'bug'], 'bug'),
        ([], ['feature'], None),
        ([], [], None),
        (['size:M', 'lgtm', 'ci'], ['size:M', 'lgtm', 'ci'], None),
        (['Bug'], ['Bug'], 'bug'),
        (['feature'], [None, 'feature'], 'feature'),
        (['feature'], [None], 'feature'),
        (['bug'], ['feature', 'bug'], 'bug'),
        (['lgtm'], ['feature'], None),
        (
            ['bug', 'size:s', 'area:test', 'priority:low', 'status:triaged'],
            ['size:s', 'area:test', 'priority:low', 'status:triaged', 'lgtm'],
            'bug',
        ),
        (['refactor', 'size:M', 'lgtm'], ['size:M', 'lgtm'], 'refactor'),
        (['feature', 'bug'], [], 'feature'),
        (['feature', 'bug'], ['bug'], 'bug'),
        (['enhancement'], [], 'enhancement'),
    ],
)
def test_label_extraction(current, timeline, expected):
    assert _parse(current, timeline) == expected


def test_no_repo_config_yields_default_scoring_label():
    """Without repo_config, default label_multipliers apply."""
    assert _parse(['feature'], ['feature'], repo_config=None) == 'feature'


def test_missing_labels_key_does_not_crash():
    """Backward-compat: payloads without the new 'labels' key yield None."""
    payload = _pr_payload([], ['feature'])
    del payload['labels']
    pr = PullRequest.from_graphql_response(
        payload, uid=0, hotkey='hk', github_id='gh', repo_config=_REPO_WITH_LEGACY_LABELS
    )
    assert pr.label is None


@pytest.mark.parametrize(
    'label,mapping,expected',
    [
        ('kind/feature', {'kind/feature': 1.5}, 1.5),
        ('kind/feature', {'kind/*': 1.5}, 1.5),
        ('type/perf', {'type/*': 1.1}, 1.1),
        ('3.0-dev', {'*-dev': 0.5}, 0.5),
        ('v2-dev', {'*-dev': 0.5}, 0.5),
        ('no-match', {'kind/*': 1.1}, None),
    ],
)
def test_max_multiplier_fnmatch(label, mapping, expected):
    got = max_multiplier_for_label(label, mapping)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_max_multiplier_picks_highest_when_multiple_patterns_match():
    assert max_multiplier_for_label('type/perf', {'type/*': 1.1, 'type/perf': 2.0}) == 2.0


def test_resolve_label_multiplier_no_configured_table():
    cfg = RepositoryConfig(weight=0.1)
    assert resolve_label_multiplier(cfg, None) == 1.0
    assert resolve_label_multiplier(cfg, 'anything') == 1.0


def test_resolve_label_multiplier_custom_default():
    cfg = RepositoryConfig(weight=0.1, default_label_multiplier=0.75)
    assert resolve_label_multiplier(cfg, None) == pytest.approx(0.75)
    assert resolve_label_multiplier(cfg, 'unlisted') == pytest.approx(0.75)


def test_resolve_label_multiplier_with_match():
    cfg = RepositoryConfig(
        weight=0.1,
        label_multipliers={'kind/feature': 1.5, 'type/*': 1.1},
        default_label_multiplier=1.0,
    )
    assert resolve_label_multiplier(cfg, 'kind/feature') == pytest.approx(1.5)
    assert resolve_label_multiplier(cfg, 'type/perf') == pytest.approx(1.1)


def test_calculate_pr_multipliers_legacy_path_respects_wildcards():
    from gittensor.classes import MinerEvaluation
    from gittensor.validator.oss_contributions.scoring import calculate_pr_multipliers

    raw = _pr_payload(['type/perf'], ['type/perf'])
    rc = RepositoryConfig(weight=0.2, label_multipliers={'type/*': 1.1, 'kind/feature': 1.5})
    pr = PullRequest.from_graphql_response(raw, 0, 'hk', 'gh', rc)
    master = {pr.repository_full_name: rc}
    ev = MinerEvaluation(uid=0, hotkey='hk', github_id='1', github_pat='token')
    calculate_pr_multipliers(pr, ev, master)
    assert pr.label == 'type/perf'
    assert pr.label_multiplier == pytest.approx(1.1)
