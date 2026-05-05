import pytest

from gittensor.classes import PullRequest
from gittensor.validator.oss_contributions.scoring import _resolve_label
from gittensor.validator.utils.load_weights import RepositoryConfig, resolve_label_multiplier

# Repo config that mirrors the old global LABEL_MULTIPLIERS table, used to keep
# existing extraction test expectations unchanged.
_LEGACY_CONFIG = RepositoryConfig(
    weight=1.0,
    label_multipliers={
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
    },
)


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


def _parse(current, timeline, config=_LEGACY_CONFIG):
    """Construct a PR and run label resolution with *config*, return resolved label."""
    pr = PullRequest.from_graphql_response(_pr_payload(current, timeline), uid=0, hotkey='hk', github_id='gh')
    resolved_label, _ = _resolve_label(pr.label, pr.current_labels, config)
    return resolved_label


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
    """Backward-compat: payloads without the 'labels' key yield no resolved label."""
    payload = _pr_payload([], ['feature'])
    del payload['labels']
    pr = PullRequest.from_graphql_response(payload, uid=0, hotkey='hk', github_id='gh')
    resolved, _ = _resolve_label(pr.label, pr.current_labels, _LEGACY_CONFIG)
    assert resolved is None


# ============================================================================
# resolve_label_multiplier — fnmatch pattern matching
# ============================================================================


@pytest.mark.parametrize(
    'pattern,label,expected',
    [
        # Exact matches
        ('bug', 'bug', 1.25),
        ('feature', 'feature', 1.50),
        # Wildcard: prefix glob
        ('kind/*', 'kind/feature', 1.5),
        ('kind/*', 'kind/bug-fix', 1.5),
        ('kind/*', 'feature', None),
        # Wildcard: suffix glob
        ('*-dev', 'backend-dev', 0.5),
        ('*-dev', 'frontend-dev', 0.5),
        ('*-dev', 'backend-prod', None),
        # Wildcard: type: prefix
        ('type:*', 'type:bug-fix', 1.1),
        ('type:*', 'type:feature', 1.1),
        ('type:*', 'bug', None),
        # Case insensitive
        ('Bug', 'bug', 1.25),
        ('BUG', 'Bug', 1.25),
        # Release-prefixed labels
        ('3.0/*', '3.0/feature', 2.0),
        ('3.0/*', '4.0/feature', None),
        # No config → always None
        (None, 'bug', None),
    ],
)
def test_resolve_label_multiplier_patterns(pattern, label, expected):
    if pattern is None:
        config = RepositoryConfig(weight=1.0)
    else:
        config = RepositoryConfig(weight=1.0, label_multipliers={pattern: expected or 1.25})
    result = resolve_label_multiplier(label, config)
    if expected is None:
        assert result is None
    else:
        assert result == (expected or 1.25)


def test_resolve_label_multiplier_no_config():
    assert resolve_label_multiplier('feature', None) is None


def test_resolve_label_multiplier_empty_map():
    config = RepositoryConfig(weight=1.0, label_multipliers={})
    assert resolve_label_multiplier('feature', config) is None


def test_resolve_label_multiplier_first_match_wins():
    """When multiple patterns match, first wins (dict ordering)."""
    config = RepositoryConfig(weight=1.0, label_multipliers={'kind/*': 1.5, 'kind/bug': 0.5})
    assert resolve_label_multiplier('kind/bug', config) == 1.5


# ============================================================================
# _resolve_label — label + multiplier resolution from PR context
# ============================================================================


def test_resolve_label_uses_candidate_first():
    config = RepositoryConfig(weight=1.0, label_multipliers={'bug': 1.25, 'feature': 1.5})
    label, mult = _resolve_label('bug', frozenset({'bug', 'feature'}), config)
    assert label == 'bug'
    assert mult == 1.25


def test_resolve_label_falls_back_when_candidate_unmatched():
    config = RepositoryConfig(weight=1.0, label_multipliers={'bug': 1.25})
    label, mult = _resolve_label('lgtm', frozenset({'lgtm', 'bug'}), config)
    assert label == 'bug'
    assert mult == 1.25


def test_resolve_label_highest_multiplier_wins_from_current():
    config = RepositoryConfig(weight=1.0, label_multipliers={'bug': 1.25, 'feature': 1.5})
    label, mult = _resolve_label(None, frozenset({'bug', 'feature'}), config)
    assert label == 'feature'
    assert mult == 1.5


def test_resolve_label_returns_default_when_no_match():
    config = RepositoryConfig(weight=1.0, label_multipliers={'bug': 1.25}, default_label_multiplier=0.8)
    label, mult = _resolve_label(None, frozenset({'lgtm'}), config)
    assert label is None
    assert mult == 0.8


def test_resolve_label_default_mult_when_no_config():
    label, mult = _resolve_label(None, frozenset({'feature'}), None)
    assert label is None
    assert mult == 1.0


# ============================================================================
# RepositoryConfig JSON data constraints
# ============================================================================


@pytest.mark.parametrize('value', [0.0, 0.01, 1.0, 10.0, 20.0])
def test_label_multiplier_value_valid_range(value):
    """Values within [0.0, 20.0] are accepted."""
    config = RepositoryConfig(weight=1.0, label_multipliers={'bug': value})
    assert resolve_label_multiplier('bug', config) == value


@pytest.mark.parametrize('value', [-0.01, 20.01, 100.0, -1.0])
def test_label_multiplier_value_out_of_range_not_enforced_at_config_level(value):
    """RepositoryConfig itself does not clamp; validation is in load_master_repo_weights."""
    config = RepositoryConfig(weight=1.0, label_multipliers={'bug': value})
    # The dataclass accepts any float; range enforcement happens during JSON loading.
    assert resolve_label_multiplier('bug', config) == value


@pytest.mark.parametrize('count', [1, 5, 10])
def test_label_multipliers_within_entry_limit(count):
    lm = {f'label-{i}': 1.0 for i in range(count)}
    config = RepositoryConfig(weight=1.0, label_multipliers=lm)
    assert config.label_multipliers is not None
    assert len(config.label_multipliers) == count


@pytest.mark.parametrize('default', [0.0, 1.0, 5.0, 20.0])
def test_default_label_multiplier_valid(default):
    config = RepositoryConfig(weight=1.0, default_label_multiplier=default)
    _, mult = _resolve_label(None, frozenset(), config)
    assert mult == default


# ============================================================================
# load_master_repo_weights constraint enforcement
# ============================================================================


def _load_from_dict(repo_dict: dict):
    """Exercise load_master_repo_weights via a temp JSON file."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from gittensor.validator.utils.load_weights import load_master_repo_weights

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(repo_dict, f)
        tmp_path = Path(f.name)

    weights_dir = tmp_path.parent
    with patch('gittensor.validator.utils.load_weights._get_weights_dir', return_value=weights_dir):
        # Rename to match the expected filename
        target = weights_dir / 'master_repositories.json'
        tmp_path.rename(target)
        result = load_master_repo_weights()
        target.unlink(missing_ok=True)
    return result


def test_load_rejects_more_than_10_entries():
    lm = {f'label-{i}': 1.0 for i in range(11)}
    repos = _load_from_dict({'owner/repo': {'weight': 1.0, 'label_multipliers': lm}})
    config = repos.get('owner/repo')
    assert config is not None
    assert config.label_multipliers is None


def test_load_skips_out_of_range_entry():
    repos = _load_from_dict({'owner/repo': {'weight': 1.0, 'label_multipliers': {'bug': 25.0, 'feature': 1.5}}})
    config = repos.get('owner/repo')
    assert config is not None
    assert config.label_multipliers is not None
    assert 'bug' not in config.label_multipliers
    assert config.label_multipliers.get('feature') == 1.5


def test_load_resets_out_of_range_default():
    repos = _load_from_dict({'owner/repo': {'weight': 1.0, 'default_label_multiplier': 99.0}})
    config = repos.get('owner/repo')
    assert config is not None
    assert config.default_label_multiplier == 1.0


def test_load_accepts_valid_config():
    repos = _load_from_dict(
        {
            'owner/repo': {
                'weight': 1.0,
                'label_multipliers': {'kind/*': 1.5, 'type:*': 1.1},
                'default_label_multiplier': 0.8,
            }
        }
    )
    config = repos.get('owner/repo')
    assert config is not None
    assert config.label_multipliers == {'kind/*': 1.5, 'type:*': 1.1}
    assert config.default_label_multiplier == 0.8


# ============================================================================
# End-to-end wildcard matching tests
# ============================================================================


@pytest.mark.parametrize(
    'label_multipliers,current,timeline,expected_label,expected_mult',
    [
        # kind/* pattern
        ({'kind/*': 1.5}, ['kind/feature'], ['kind/feature'], 'kind/feature', 1.5),
        ({'kind/*': 1.5}, ['kind/bug-fix', 'kind/feature'], ['kind/bug-fix', 'kind/feature'], 'kind/feature', 1.5),
        ({'kind/*': 1.5}, ['size:m', 'kind/feature'], ['size:m', 'kind/feature'], 'kind/feature', 1.5),
        # type:* pattern
        ({'type:*': 1.1}, ['type:bug-fix'], ['type:bug-fix'], 'type:bug-fix', 1.1),
        # *-dev suffix pattern
        ({'*-dev': 0.5}, ['backend-dev'], ['backend-dev'], 'backend-dev', 0.5),
        ({'*-dev': 0.5}, ['frontend-prod'], ['frontend-prod'], None, 1.0),
        # Release-prefixed labels
        ({'3.0/*': 2.0}, ['3.0/feature'], ['3.0/feature'], '3.0/feature', 2.0),
        ({'3.0/*': 2.0}, ['4.0/feature'], ['4.0/feature'], None, 1.0),
        # Multiple patterns: last applied wins if it matches
        (
            {'kind/*': 1.5, 'type:*': 1.1},
            ['kind/feature', 'type:bug-fix'],
            ['kind/feature', 'type:bug-fix'],
            'type:bug-fix',
            1.1,
        ),
        # No match → default multiplier applied
        ({'kind/*': 1.5}, ['lgtm'], ['lgtm'], None, 1.0),
    ],
)
def test_e2e_wildcard_matching(label_multipliers, current, timeline, expected_label, expected_mult):
    config = RepositoryConfig(weight=1.0, label_multipliers=label_multipliers)
    pr = PullRequest.from_graphql_response(_pr_payload(current, timeline), uid=0, hotkey='hk', github_id='gh')
    resolved_label, resolved_mult = _resolve_label(pr.label, pr.current_labels, config)
    assert resolved_label == expected_label
    assert resolved_mult == pytest.approx(expected_mult)
