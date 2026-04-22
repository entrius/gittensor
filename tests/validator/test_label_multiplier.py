import pytest

from gittensor.classes import PullRequest
from gittensor.constants import LABEL_MULTIPLIERS


@pytest.mark.parametrize('label,expected', list(LABEL_MULTIPLIERS.items()))
def test_known_labels(label, expected):
    assert LABEL_MULTIPLIERS.get(label, 1.0) == expected


@pytest.mark.parametrize('label', ['docs', 'question', 'wontfix', 'kind/feature'])
def test_unknown_labels_return_default(label):
    assert LABEL_MULTIPLIERS.get(label, 1.0) == 1.0


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
    return PullRequest.from_graphql_response(_pr_payload(current, timeline), uid=0, hotkey='hk', github_id='gh').label


@pytest.mark.parametrize(
    'current,timeline,expected',
    [
        # Single scoring label — unchanged behavior
        (['feature'], ['feature'], 'feature'),
        # dify case: refactor masked by lgtm added after — fix picks refactor
        (['size:M', 'refactor', 'lgtm'], ['size:M', 'refactor', 'lgtm'], 'refactor'),
        # godot case: bug masked by topic:* added after — fix picks bug
        (['bug', 'topic:editor', 'topic:shaders'], ['bug', 'topic:editor', 'topic:shaders'], 'bug'),
        # Reclassification: feature then bug — last scoring label wins
        (['feature', 'bug'], ['feature', 'bug'], 'bug'),
        # Label added then removed — not in current, skipped
        ([], ['feature'], None),
        # No labels
        ([], [], None),
        # Only non-scoring labels
        (['size:M', 'lgtm', 'ci'], ['size:M', 'lgtm', 'ci'], None),
        # Case insensitive
        (['Bug'], ['Bug'], 'bug'),
        # Null label node (PR #553 — deleted repo label)
        (['feature'], [None, 'feature'], 'feature'),
        (['feature'], [None], 'feature'),
        # Remove-and-replace: feature removed, bug added
        (['bug'], ['feature', 'bug'], 'bug'),
        # Only scoring label was removed
        (['lgtm'], ['feature'], None),
        # Timeline truncated: scoring label in current but not in last-5 events (#646)
        (
            ['bug', 'size:s', 'area:test', 'priority:low', 'status:triaged'],
            ['size:s', 'area:test', 'priority:low', 'status:triaged', 'lgtm'],
            'bug',
        ),
        # Timeline truncated: penalty label still applied via fallback
        (['refactor', 'size:M', 'lgtm'], ['size:M', 'lgtm'], 'refactor'),
        # Timeline truncated: multiple scoring labels both truncated — highest multiplier wins
        (['feature', 'bug'], [], 'feature'),
        # Timeline partially truncated: one scoring label in timeline wins over truncated one
        (['feature', 'bug'], ['bug'], 'bug'),
        # Empty timeline with scoring label in current
        (['enhancement'], [], 'enhancement'),
    ],
)
def test_label_extraction(current, timeline, expected):
    assert _parse(current, timeline) == expected


def test_missing_labels_key_does_not_crash():
    """Backward-compat: payloads without the new 'labels' key yield None."""
    payload = _pr_payload([], ['feature'])
    del payload['labels']
    pr = PullRequest.from_graphql_response(payload, uid=0, hotkey='hk', github_id='gh')
    assert pr.label is None
