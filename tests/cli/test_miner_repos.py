# Entrius 2025

"""Tests for the `gitt miner repos` CLI command and its scoring-weight helpers."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.miner_commands.repos import (
    _build_rows,
    _filter_repos,
    _rank_repos,
    _summarize,
    _weight_tier,
)
from gittensor.validator.utils.load_weights import RepositoryConfig

REPOS_PATH = 'gittensor.cli.miner_commands.repos.load_master_repo_weights'


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_repos():
    """A small fixture set: three active repos of varying weight plus one delisted."""
    return {
        'owner/high': RepositoryConfig(weight=0.50),
        'owner/medium': RepositoryConfig(weight=0.20),
        'owner/low': RepositoryConfig(weight=0.05),
        'owner/delisted': RepositoryConfig(weight=0.30, inactive_at='2025-01-01T00:00:00Z'),
    }


class TestFilterRepos:
    def test_excludes_inactive_by_default(self, sample_repos):
        result = _filter_repos(sample_repos, include_inactive=False, search=None)
        assert 'owner/delisted' not in result
        assert len(result) == 3

    def test_includes_inactive_when_requested(self, sample_repos):
        result = _filter_repos(sample_repos, include_inactive=True, search=None)
        assert 'owner/delisted' in result
        assert len(result) == 4

    def test_search_is_case_insensitive_substring(self, sample_repos):
        result = _filter_repos(sample_repos, include_inactive=True, search='HIGH')
        assert set(result) == {'owner/high'}

    def test_search_matches_nothing(self, sample_repos):
        result = _filter_repos(sample_repos, include_inactive=True, search='nonexistent')
        assert result == {}

    def test_search_combines_with_active_filter(self, sample_repos):
        # 'owner' matches all, but delisted is excluded when include_inactive is False.
        result = _filter_repos(sample_repos, include_inactive=False, search='owner')
        assert 'owner/delisted' not in result
        assert len(result) == 3


class TestRankRepos:
    def test_sorted_by_weight_descending(self, sample_repos):
        ranked = _rank_repos(_filter_repos(sample_repos, include_inactive=False, search=None))
        weights = [config.weight for _, config in ranked]
        assert weights == sorted(weights, reverse=True)
        assert ranked[0][0] == 'owner/high'

    def test_ties_broken_alphabetically(self):
        repos = {
            'owner/zebra': RepositoryConfig(weight=0.10),
            'owner/apple': RepositoryConfig(weight=0.10),
        }
        ranked = _rank_repos(repos)
        assert [name for name, _ in ranked] == ['owner/apple', 'owner/zebra']


class TestWeightTier:
    def test_tier_boundaries(self):
        assert _weight_tier(0.01) == 'high'
        assert _weight_tier(0.0075) == 'high'
        assert _weight_tier(0.0074) == 'medium'
        assert _weight_tier(0.0030) == 'medium'
        assert _weight_tier(0.0029) == 'low'
        assert _weight_tier(0.0) == 'low'


class TestSummarize:
    def test_counts_and_total_weight(self, sample_repos):
        ranked = _rank_repos(_filter_repos(sample_repos, include_inactive=True, search=None))
        total_weight = sum(c.weight for _, c in ranked)
        summary = _summarize(ranked, total_weight)
        assert summary['total'] == 4
        assert summary['active'] == 3
        assert summary['inactive'] == 1
        assert summary['total_weight'] == pytest.approx(1.05)
        assert sum(summary['tiers'].values()) == 4

    def test_zero_total_weight_does_not_divide_by_zero(self):
        repos = {'owner/zero': RepositoryConfig(weight=0.0)}
        ranked = _rank_repos(repos)
        summary = _summarize(ranked, 0.0)
        assert summary['tiers']['low'] == 1
        assert summary['total_weight'] == 0.0


class TestBuildRows:
    def test_rows_have_rank_share_and_tier(self, sample_repos):
        ranked = _rank_repos(_filter_repos(sample_repos, include_inactive=False, search=None))
        total_weight = sum(c.weight for _, c in ranked)
        rows = _build_rows(ranked, total_weight, top=None)
        assert rows[0]['rank'] == 1
        assert rows[0]['repository'] == 'owner/high'
        # 0.50 / 0.75 active total
        assert rows[0]['share'] == pytest.approx(0.5 / 0.75)
        assert all('tier' in r and 'active' in r for r in rows)

    def test_top_n_truncates(self, sample_repos):
        ranked = _rank_repos(_filter_repos(sample_repos, include_inactive=True, search=None))
        total_weight = sum(c.weight for _, c in ranked)
        rows = _build_rows(ranked, total_weight, top=2)
        assert len(rows) == 2
        assert [r['rank'] for r in rows] == [1, 2]


class TestMinerReposCommand:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'repos', '--help'])
        assert result.exit_code == 0
        assert 'ranked by scoring weight' in result.output

    def test_alias(self, runner):
        result = runner.invoke(cli, ['m', 'repos', '--help'])
        assert result.exit_code == 0

    def test_table_output(self, runner, sample_repos):
        with patch(REPOS_PATH, return_value=sample_repos):
            result = runner.invoke(cli, ['miner', 'repos'])
        assert result.exit_code == 0
        assert 'owner/high' in result.output
        assert 'owner/delisted' not in result.output  # active-only default

    def test_all_flag_includes_delisted(self, runner, sample_repos):
        with patch(REPOS_PATH, return_value=sample_repos):
            result = runner.invoke(cli, ['miner', 'repos', '--all'])
        assert result.exit_code == 0
        assert 'owner/delisted' in result.output

    def test_json_output_shape(self, runner, sample_repos):
        with patch(REPOS_PATH, return_value=sample_repos):
            result = runner.invoke(cli, ['miner', 'repos', '--all', '--json-output'])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload['summary']['total'] == 4
        assert len(payload['repositories']) == 4
        assert payload['repositories'][0]['repository'] == 'owner/high'

    def test_top_flag(self, runner, sample_repos):
        with patch(REPOS_PATH, return_value=sample_repos):
            result = runner.invoke(cli, ['miner', 'repos', '--top', '1', '--json-output'])
        payload = json.loads(result.output)
        assert len(payload['repositories']) == 1

    def test_search_flag(self, runner, sample_repos):
        with patch(REPOS_PATH, return_value=sample_repos):
            result = runner.invoke(cli, ['miner', 'repos', '--all', '--search', 'medium', '--json-output'])
        payload = json.loads(result.output)
        assert [r['repository'] for r in payload['repositories']] == ['owner/medium']

    def test_search_no_match_table(self, runner, sample_repos):
        with patch(REPOS_PATH, return_value=sample_repos):
            result = runner.invoke(cli, ['miner', 'repos', '--search', 'zzz'])
        assert result.exit_code == 0
        assert 'No repositories match' in result.output

    def test_empty_weights_file_errors(self, runner):
        with patch(REPOS_PATH, return_value={}):
            result = runner.invoke(cli, ['miner', 'repos'])
        assert result.exit_code != 0
        assert 'No repositories found' in result.output

    def test_empty_weights_file_json_error(self, runner):
        with patch(REPOS_PATH, return_value={}):
            result = runner.invoke(cli, ['miner', 'repos', '--json-output'])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload['success'] is False
