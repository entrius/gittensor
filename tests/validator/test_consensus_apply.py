# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for overlaying consensus shares onto the baked repository registry."""

from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.consensus import apply_consensus


def _master():
    return {
        'a/tuned': RepositoryConfig(emission_share=0.6, maintainer_cut=0.2, default_label_multiplier=1.5),
        'b/plain': RepositoryConfig(emission_share=0.4),
    }


class TestApplyConsensus:
    def test_overrides_share_keeps_tuned_config(self):
        result = apply_consensus(_master(), {'a/tuned': 0.7, 'b/plain': 0.3})
        assert result['a/tuned'].emission_share == 0.7
        assert result['a/tuned'].maintainer_cut == 0.2
        assert result['a/tuned'].default_label_multiplier == 1.5

    def test_zeroes_master_repos_absent_from_aggregate(self):
        result = apply_consensus(_master(), {'a/tuned': 1.0})
        assert result['b/plain'].emission_share == 0.0

    def test_adds_novel_repo_with_defaults(self):
        result = apply_consensus(_master(), {'new/comer': 0.5, 'a/tuned': 0.5})
        novel = result['new/comer']
        assert novel.emission_share == 0.5
        assert novel.maintainer_cut == 0.0
        assert novel.default_label_multiplier == 1.0

    def test_none_shares_returns_master_unchanged(self):
        master = _master()
        assert apply_consensus(master, None) is master

    def test_does_not_mutate_master_configs(self):
        master = _master()
        apply_consensus(master, {'a/tuned': 1.0})
        assert master['a/tuned'].emission_share == 0.6
