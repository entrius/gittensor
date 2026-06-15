# Entrius 2025

"""Tests for `gitt repo simulate` (emission what-if for a proposed reweight).

These drive the real emission-allocation functions end-to-end through the CLI;
only the bittensor SDK surface is stubbed (by the CLI conftest), so the numbers
asserted here are the production split.
"""

import json

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj), encoding='utf-8')
    return str(path)


def _config(tmp_path, configs, name='config.json'):
    return _write(tmp_path, name, configs)


def _scenario(tmp_path, scenario, name='scenario.json'):
    return _write(tmp_path, name, scenario)


def _run(runner, *args):
    return runner.invoke(cli, ['repo', 'simulate', *args])


class TestSimulateCommand:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['repo', 'simulate', '--help'])
        assert result.exit_code == 0
        assert 'emission split' in result.output
        assert '--scenario' in result.output

    def test_alias_repo_r(self, runner):
        result = runner.invoke(cli, ['r', 'simulate', '--help'])
        assert result.exit_code == 0

    def test_missing_scenario_file(self, runner, tmp_path):
        result = _run(runner, '--scenario', str(tmp_path / 'nope.json'), '--json')
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload['success'] is False
        assert 'not found' in payload['error']['message']

    def test_json_split_matches_production_math(self, runner, tmp_path):
        """A single miner holding all the score in one repo: assert the exact
        repo_slice / pr_slice / issue_slice and the recycle + treasury split."""
        config = _config(tmp_path, {'octo/repo': {'emission_share': 0.2, 'issue_discovery_share': 0.25}})
        scenario = _scenario(
            tmp_path, {'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 50.0, 'issue_score': 8.0}}}]}
        )
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload['success'] is True

        rows = payload['allocations']
        assert len(rows) == 1
        row = rows[0]
        assert row['repository_full_name'] == 'octo/repo'
        assert row['repo_slice'] == pytest.approx(0.18)
        assert row['pr_slice'] == pytest.approx(0.135)
        assert row['issue_discovery_slice'] == pytest.approx(0.045)
        assert row['pr_rewards']['5'] == pytest.approx(0.135)
        assert row['issue_discovery_rewards']['5'] == pytest.approx(0.045)
        assert row['recycled'] is False

        miners = {m['uid']: m for m in payload['miners']}
        assert miners[5]['emission'] == pytest.approx(0.18)
        # The unconfigured 0.8 of the registry recycles to UID 0; treasury is flat 10%.
        assert miners[0]['emission'] == pytest.approx(0.72)
        assert miners[0]['role'] == 'recycled'
        assert miners[111]['emission'] == pytest.approx(0.10)
        assert miners[111]['role'] == 'issues_treasury'

    def test_issue_only_repo(self, runner, tmp_path):
        """issue_discovery_share=1.0 routes the whole slice to the issue pool."""
        config = _config(tmp_path, {'octo/repo': {'emission_share': 0.5, 'issue_discovery_share': 1.0}})
        scenario = _scenario(tmp_path, {'miners': [{'uid': 9, 'repos': {'octo/repo': {'issue_score': 4.0}}}]})
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)['allocations'][0]
        assert row['pr_slice'] == pytest.approx(0.0)
        assert row['issue_discovery_slice'] == pytest.approx(0.45)
        assert row['issue_discovery_rewards']['9'] == pytest.approx(0.45)

    def test_maintainer_carve_out(self, runner, tmp_path):
        """A maintainer_cut repo with a declared maintainer carves off the top."""
        config = _config(
            tmp_path,
            {'octo/repo': {'emission_share': 0.2, 'issue_discovery_share': 0.0, 'maintainer_cut': 0.5}},
        )
        scenario = _scenario(
            tmp_path,
            {
                'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 42.5}}}],
                'maintainers': {'octo/repo': [5]},
            },
        )
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)['allocations'][0]
        assert row['maintainer_carve_out'] == pytest.approx(0.09)
        assert row['maintainer_rewards']['5'] == pytest.approx(0.09)
        assert row['pr_slice'] == pytest.approx(0.09)
        assert row['pr_rewards']['5'] == pytest.approx(0.09)

    def test_empty_repo_recycles_full_slice(self, runner, tmp_path):
        """A configured repo with no scorers recycles its entire slice."""
        config = _config(tmp_path, {'octo/repo': {'emission_share': 0.3}})
        scenario = _scenario(tmp_path, {'miners': [{'uid': 5, 'repos': {'other/repo': {'pr_score': 10.0}}}]})
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        row = payload['allocations'][0]
        assert row['recycled'] is True
        assert row['recycled_amount'] == pytest.approx(0.27)
        # other/repo is not in the config, so it must be flagged and contribute nothing.
        assert any('absent from the config' in w for w in payload['warnings'])

    def test_reserved_uid_warning(self, runner, tmp_path):
        config = _config(tmp_path, {'octo/repo': {'emission_share': 0.2}})
        scenario = _scenario(tmp_path, {'miners': [{'uid': 0, 'repos': {'octo/repo': {'pr_score': 5.0}}}]})
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)['warnings']
        assert any('reserved UIDs' in w for w in warnings)

    def test_diff_mode(self, runner, tmp_path):
        baseline = _config(tmp_path, {'octo/repo': {'emission_share': 0.2, 'issue_discovery_share': 0.0}}, 'base.json')
        proposed = _config(tmp_path, {'octo/repo': {'emission_share': 0.4, 'issue_discovery_share': 0.0}}, 'prop.json')
        scenario = _scenario(tmp_path, {'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 10.0}}}]})
        result = _run(runner, '--scenario', scenario, '--config', proposed, '--diff', baseline, '--json')
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload['mode'] == 'diff'

        repo = {r['repository_full_name']: r for r in payload['repo_diff']}['octo/repo']
        assert repo['baseline_repo_slice'] == pytest.approx(0.18)
        assert repo['proposed_repo_slice'] == pytest.approx(0.36)
        assert repo['delta_repo_slice'] == pytest.approx(0.18)

        miner = {m['uid']: m for m in payload['miner_diff']}
        assert miner[5]['delta'] == pytest.approx(0.18)
        # The extra 0.2 emission_share comes out of recycle.
        assert miner[0]['delta'] == pytest.approx(-0.18)

    def test_invalid_config_surfaces_validation_error(self, runner, tmp_path):
        config = _config(tmp_path, {'octo/repo': {'emission_share': 2.0}})
        scenario = _scenario(tmp_path, {'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 1.0}}}]})
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload['success'] is False
        assert 'emission_share' in payload['error']['message']

    def test_missing_config_path(self, runner, tmp_path):
        scenario = _scenario(tmp_path, {'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 1.0}}}]})
        result = _run(runner, '--scenario', scenario, '--config', str(tmp_path / 'gone.json'), '--json')
        assert result.exit_code == 1
        assert 'config file not found' in json.loads(result.output)['error']['message']

    def test_bad_score_type_rejected(self, runner, tmp_path):
        config = _config(tmp_path, {'octo/repo': {'emission_share': 0.2}})
        scenario = _scenario(tmp_path, {'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 'lots'}}}]})
        result = _run(runner, '--scenario', scenario, '--config', config, '--json')
        assert result.exit_code == 1
        assert 'must be a number' in json.loads(result.output)['error']['message']

    def test_table_output_renders(self, runner, tmp_path):
        config = _config(tmp_path, {'octo/repo': {'emission_share': 0.2, 'issue_discovery_share': 0.25}})
        scenario = _scenario(
            tmp_path, {'miners': [{'uid': 5, 'repos': {'octo/repo': {'pr_score': 50.0, 'issue_score': 8.0}}}]}
        )
        result = _run(runner, '--scenario', scenario, '--config', config)
        assert result.exit_code == 0, result.output
        assert 'Per-repository emission allocation' in result.output
        assert 'Per-miner emission totals' in result.output
        assert 'octo/repo' in result.output
