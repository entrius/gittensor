# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Veto-label semantics for repository label multipliers.

A label configured with multiplier ``0.0`` is a rejection / void marker
(``eval:REJECT``, ``invalid-pr``, ``duplicate-pr``, ``slop``, ``kata:invalid``,
...). It must zero the PR's label multiplier even when a higher-valued label is
applied alongside it, otherwise a maintainer can never actually revoke a PR that
already earned a reward label.
"""

import pytest

from gittensor.validator.oss_contributions.label_resolution import (
    get_label_multiplier,
    resolve_highest_label_multiplier,
)
from gittensor.validator.utils.load_weights import RepositoryConfig


class TestVetoBeatsRewardLabel:
    """A 0.0 label co-applied with a positive label wins (result is 0.0)."""

    def test_reject_label_vetoes_eval_reward(self):
        # Mirrors the sparkinfer / SparkDistill / cuda-compute-oss registries.
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'eval:L': 2.5, 'eval:REJECT': 0.0},
        )
        label, multiplier = resolve_highest_label_multiplier(['eval:L', 'eval:REJECT'], config)
        assert multiplier == pytest.approx(0.0)
        assert label == 'eval:REJECT'

    def test_invalid_pr_vetoes_round_winner(self):
        # Mirrors the gt-imagent registry (round-winner 10.0, invalid-pr 0.0).
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'round-winner': 10.0, 'invalid-pr': 0.0, 'duplicate-pr': 0.0},
        )
        _, multiplier = resolve_highest_label_multiplier(['round-winner', 'invalid-pr'], config)
        assert multiplier == pytest.approx(0.0)

    def test_slop_vetoes_priority(self):
        # Mirrors the JSONbored registries (gittensor:priority 1.5, slop 0.0).
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'gittensor:priority': 1.5, 'slop': 0.0},
        )
        _, multiplier = resolve_highest_label_multiplier(['gittensor:priority', 'slop'], config)
        assert multiplier == pytest.approx(0.0)

    def test_veto_independent_of_label_order(self):
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'eval:L': 2.5, 'eval:REJECT': 0.0},
        )
        forward = resolve_highest_label_multiplier(['eval:L', 'eval:REJECT'], config)
        reverse = resolve_highest_label_multiplier(['eval:REJECT', 'eval:L'], config)
        assert forward == reverse
        assert forward[1] == pytest.approx(0.0)

    def test_wildcard_veto_beats_wildcard_reward(self):
        # Mirrors the kata registry (kata:winner:* 0.7, kata:invalid 0.0) but
        # exercises the wildcard path on both sides.
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'kata:winner:*': 0.7, 'kata:defeat:*': 0.0},
        )
        _, multiplier = resolve_highest_label_multiplier(['kata:winner:s1', 'kata:defeat:s1'], config)
        assert multiplier == pytest.approx(0.0)


class TestSingleLabelMatchingBothVetoAndReward:
    """One label matching a 0.0 pattern and a positive pattern resolves to 0.0."""

    def test_catch_all_reward_plus_specific_veto(self):
        # A repo adds an `eval:*` catch-all alongside an explicit `eval:REJECT`
        # veto; the single label `eval:REJECT` matches both patterns.
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'eval:*': 3.0, 'eval:REJECT': 0.0},
        )
        assert get_label_multiplier('eval:REJECT', config) == pytest.approx(0.0)
        # A non-veto label under the same catch-all still scores.
        assert get_label_multiplier('eval:L', config) == pytest.approx(3.0)


class TestNonVetoBehaviorUnchanged:
    """Positive-only resolution keeps its previous 'highest wins' semantics."""

    def test_highest_positive_still_wins(self):
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'bug': 1.2, 'enhancement': 1.3, 'feature': 1.0},
        )
        label, multiplier = resolve_highest_label_multiplier(['bug', 'enhancement'], config)
        assert label == 'enhancement'
        assert multiplier == pytest.approx(1.3)

    def test_low_positive_is_not_a_veto(self):
        # 0.05 is a low reward, not a rejection: it must not veto a higher label.
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'mult:contribution': 0.05, 'perf:l': 2.5},
        )
        _, multiplier = resolve_highest_label_multiplier(['mult:contribution', 'perf:l'], config)
        assert multiplier == pytest.approx(2.5)

    def test_default_multiplier_used_when_no_pattern_matches(self):
        config = RepositoryConfig(
            emission_share=1.0,
            label_multipliers={'bug': 1.2},
            default_label_multiplier=0.8,
        )
        label, multiplier = resolve_highest_label_multiplier(['unrelated'], config)
        assert label is None
        assert multiplier == pytest.approx(0.8)
