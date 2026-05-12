import pytest

from gittensor.classes import MinerEvaluation, PullRequest
from gittensor.validator.oss_contributions.label_resolution import (
    get_label_multiplier,
    resolve_highest_label_multiplier,
    resolve_legacy_label_multiplier,
)
from gittensor.validator.oss_contributions.scoring import calculate_pr_multipliers
from gittensor.validator.utils.load_weights import RepositoryConfig


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


def _parse(current, timeline):
    return PullRequest.from_graphql_response(_pr_payload(current, timeline), uid=0, hotkey='hk', github_id='gh')


def _score(current, timeline, repo_config):
    pr = _parse(current, timeline)
    calculate_pr_multipliers(pr, MinerEvaluation(uid=0, hotkey='hk', github_id='gh'), {'x/y': repo_config})
    return pr


@pytest.mark.parametrize(
    'pattern,label,expected',
    [
        ('kind/feature', 'kind/feature', 1.5),
        ('kind/*', 'kind/bug', 1.25),
        ('type:*', 'type:bug-fix', 1.1),
        ('*-dev', 'backend-dev', 0.5),
        ('3.*/feature', '3.0/feature', 1.5),
        ('Bug', 'bug', 1.25),
    ],
)
def test_get_label_multiplier_matches_repo_patterns(pattern, label, expected):
    config = RepositoryConfig(emission_share=1.0, label_multipliers={pattern: expected})

    assert get_label_multiplier(label, config) == pytest.approx(expected)


def test_get_label_multiplier_returns_highest_matching_pattern():
    config = RepositoryConfig(
        emission_share=1.0,
        label_multipliers={
            'kind/*': 1.1,
            'kind/feature': 1.5,
            '*/feature': 1.25,
        },
    )

    assert get_label_multiplier('kind/feature', config) == pytest.approx(1.5)


def test_get_label_multiplier_returns_none_without_repo_config_match():
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'kind/*': 1.5})

    assert get_label_multiplier('feature', config) is None
    assert get_label_multiplier('kind/feature', RepositoryConfig(emission_share=1.0)) is None
    assert get_label_multiplier('kind/feature', None) is None


def test_highest_label_resolution_uses_default_when_no_candidate_matches():
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'kind/*': 1.5}, default_label_multiplier=0.8)

    label, multiplier = resolve_highest_label_multiplier(['feature', 'bug'], config)

    assert label is None
    assert multiplier == pytest.approx(0.8)


def test_highest_label_resolution_tiebreaks_by_label_name():
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'a': 1.5, 'b': 1.5})

    label, multiplier = resolve_highest_label_multiplier(['a', 'b'], config)

    assert label == 'b'
    assert multiplier == pytest.approx(1.5)


@pytest.mark.parametrize(
    'current,timeline,expected_label,expected_multiplier',
    [
        (['kind/feature'], ['kind/feature'], 'kind/feature', 1.5),
        (['type:bug-fix'], ['type:bug-fix'], 'type:bug-fix', 1.1),
        (['backend-dev'], ['backend-dev'], 'backend-dev', 0.5),
        (['3.0/feature'], ['3.0/feature'], '3.0/feature', 2.0),
    ],
)
def test_wildcard_label_resolves_through_legacy_scoring(current, timeline, expected_label, expected_multiplier):
    config = RepositoryConfig(
        emission_share=1.0,
        label_multipliers={
            'kind/*': 1.5,
            'type:*': 1.1,
            '*-dev': 0.5,
            '3.0/*': 2.0,
        },
    )

    pr = _score(current, timeline, config)

    assert pr.label == expected_label
    assert pr.label_multiplier == pytest.approx(expected_multiplier)


def test_repo_without_label_multipliers_ignores_old_global_label():
    pr = _score(['feature'], ['feature'], RepositoryConfig(emission_share=1.0))

    assert pr.label is None
    assert pr.label_multiplier == pytest.approx(1.0)


def test_repo_default_label_multiplier_applies_without_label_map():
    config = RepositoryConfig(emission_share=1.0, default_label_multiplier=0.8)

    labeled = _score(['feature'], ['feature'], config)
    unlabeled = _score([], [], config)

    assert labeled.label is None
    assert labeled.label_multiplier == pytest.approx(0.8)
    assert unlabeled.label is None
    assert unlabeled.label_multiplier == pytest.approx(0.8)


def test_legacy_timeline_order_preserved_for_matching_labels():
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'feature': 1.5, 'bug': 1.25})

    pr = _score(['feature', 'bug'], ['feature', 'bug'], config)

    assert pr.label == 'bug'
    assert pr.label_multiplier == pytest.approx(1.25)


def test_legacy_timeline_skips_unmatched_labels():
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'bug': 1.25})

    pr = _score(['lgtm', 'bug'], ['bug', 'lgtm'], config)

    assert pr.label == 'bug'
    assert pr.label_multiplier == pytest.approx(1.25)


def test_legacy_truncated_timeline_uses_highest_current_match():
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'feature': 1.5, 'bug': 1.25})

    pr = _score(['feature', 'bug'], [], config)

    assert pr.label == 'feature'
    assert pr.label_multiplier == pytest.approx(1.5)


@pytest.mark.parametrize(
    'current,timeline,expected_label,expected_multiplier',
    [
        ([], ['feature'], None, 1.0),
        ([], [], None, 1.0),
        (['size:M', 'lgtm', 'ci'], ['size:M', 'lgtm', 'ci'], None, 1.0),
        (['Bug'], ['Bug'], 'bug', 1.25),
        (['feature'], [None, 'feature'], 'feature', 1.5),
        (['feature'], [None], 'feature', 1.5),
        (['bug'], ['feature', 'bug'], 'bug', 1.25),
        (['lgtm'], ['feature'], None, 1.0),
        (['feature', 'bug'], ['bug'], 'bug', 1.25),
    ],
)
def test_legacy_current_and_timeline_compatibility(current, timeline, expected_label, expected_multiplier):
    config = RepositoryConfig(emission_share=1.0, label_multipliers={'feature': 1.5, 'bug': 1.25})

    pr = _score(current, timeline, config)

    assert pr.label == expected_label
    assert pr.label_multiplier == pytest.approx(expected_multiplier)


def test_missing_labels_key_does_not_crash():
    payload = _pr_payload([], ['feature'])
    del payload['labels']

    pr = PullRequest.from_graphql_response(payload, uid=0, hotkey='hk', github_id='gh')
    label, multiplier = resolve_legacy_label_multiplier(
        pr.label_timeline_order, pr.current_labels, RepositoryConfig(emission_share=1.0)
    )

    assert pr.current_labels == frozenset()
    assert pr.label_timeline_order == ()
    assert label is None
    assert multiplier == pytest.approx(1.0)
