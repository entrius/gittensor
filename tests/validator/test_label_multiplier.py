import pytest

from gittensor.validator.oss_contributions.label_resolution import (
    get_label_multiplier,
    resolve_highest_label_multiplier,
)
from gittensor.validator.utils.load_weights import RepositoryConfig


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
