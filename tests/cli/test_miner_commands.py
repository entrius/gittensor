# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gittensor import __version__
from gittensor.cli.main import cli
from gittensor.cli.miner_commands.helpers import _pat_check_aggregate_counts


def _fake_axon(hotkey: str = 'hk' * 16) -> MagicMock:
    """Build a minimal validator axon stand-in for `--json-output` tests."""
    axon = MagicMock()
    axon.hotkey = hotkey
    return axon


def _fake_response(**fields) -> MagicMock:
    """Build a minimal Synapse response stand-in for `--json-output` tests."""
    resp = MagicMock()
    for name, value in fields.items():
        setattr(resp, name, value)
    resp.dendrite = MagicMock()
    resp.dendrite.status_code = 200
    return resp


@pytest.fixture
def runner():
    return CliRunner()


class TestMinerPost:
    def test_no_pat_prompts_interactively(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--wallet', 'test', '--hotkey', 'test'], input='')
        assert 'Enter your GitHub Personal Access Token' in result.output

    def test_no_pat_json_mode_exits(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--json-output', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output['success'] is False

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=False)
    def test_pat_flag_used(self, mock_validate, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--pat', 'ghp_test123', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        assert 'invalid' in result.output.lower() or 'expired' in result.output.lower()
        mock_validate.assert_called_once_with('ghp_test123')

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=False)
    def test_invalid_pat_exits(self, mock_validate, runner, monkeypatch):
        monkeypatch.setenv('GITTENSOR_MINER_PAT', 'ghp_invalid')
        result = runner.invoke(cli, ['miner', 'post', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        assert 'invalid' in result.output.lower() or 'expired' in result.output.lower()

    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'post', '--help'])
        assert result.exit_code == 0
        assert 'Broadcast your GitHub PAT' in result.output

    def test_miner_alias(self, runner):
        """gitt m post should work as alias for gitt miner post."""
        result = runner.invoke(cli, ['m', 'post', '--help'])
        assert result.exit_code == 0
        assert 'Broadcast your GitHub PAT' in result.output


class TestMinerCheck:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'check', '--help'])
        assert result.exit_code == 0
        assert 'Check how many validators' in result.output

    def test_check_alias(self, runner):
        """gitt m check should work as alias for gitt miner check."""
        result = runner.invoke(cli, ['m', 'check', '--help'])
        assert result.exit_code == 0
        assert 'Check how many validators' in result.output


class TestCliVersion:
    def test_version_matches_package_version(self, runner):
        result = runner.invoke(cli, ['--version'])
        assert result.exit_code == 0
        assert result.output == f'gittensor, version {__version__}\n'


@pytest.mark.filterwarnings("ignore::RuntimeWarning:_pytest")
class TestMinerPostExitCodeOnZeroAccepted:
    """Regression tests for #841: `gitt miner post` must exit non-zero when no validator accepts.

    Suppresses the harmless "coroutine never awaited" RuntimeWarning that fires
    because we patch asyncio.run, so the inner _broadcast coroutine object never
    gets awaited — irrelevant to the exit-code contract under test.
    """

    @patch('asyncio.run')
    @patch('gittensor.cli.miner_commands.post._require_validator_axons')
    @patch('gittensor.cli.miner_commands.post._require_registered')
    @patch('gittensor.cli.miner_commands.post._connect_bittensor')
    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=True)
    def test_post_exits_non_zero_when_all_validators_reject(
        self,
        _validate,
        connect,
        _require_reg,
        require_axons,
        async_run,
        runner,
        monkeypatch,
    ):
        monkeypatch.setenv('GITTENSOR_MINER_PAT', 'ghp_fake')
        connect.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        require_axons.return_value = ([_fake_axon('hk1' * 16), _fake_axon('hk2' * 16)], [1, 2])
        async_run.return_value = [
            _fake_response(accepted=False, rejection_reason='Account too young'),
            _fake_response(accepted=False, rejection_reason='Org-restricted PAT'),
        ]

        result = runner.invoke(
            cli,
            ['miner', 'post', '--json-output', '--wallet', 'test', '--hotkey', 'test'],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload['success'] is False
        assert payload['accepted'] == 0
        assert payload['rejected'] == 2

    @patch('asyncio.run')
    @patch('gittensor.cli.miner_commands.post._require_validator_axons')
    @patch('gittensor.cli.miner_commands.post._require_registered')
    @patch('gittensor.cli.miner_commands.post._connect_bittensor')
    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=True)
    def test_post_exits_zero_when_at_least_one_validator_accepts(
        self,
        _validate,
        connect,
        _require_reg,
        require_axons,
        async_run,
        runner,
        monkeypatch,
    ):
        monkeypatch.setenv('GITTENSOR_MINER_PAT', 'ghp_fake')
        connect.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        require_axons.return_value = ([_fake_axon('hk1' * 16), _fake_axon('hk2' * 16)], [1, 2])
        async_run.return_value = [
            _fake_response(accepted=True, rejection_reason=None),
            _fake_response(accepted=False, rejection_reason='Org-restricted PAT'),
        ]

        result = runner.invoke(
            cli,
            ['miner', 'post', '--json-output', '--wallet', 'test', '--hotkey', 'test'],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload['success'] is True
        assert payload['accepted'] == 1


@pytest.mark.filterwarnings("ignore::RuntimeWarning:_pytest")
class TestMinerCheckExitCodeOnZeroValid:
    """Regression tests for #842: `gitt miner check` must exit non-zero when no validator has a valid PAT.

    Same RuntimeWarning suppression rationale as TestMinerPostExitCodeOnZeroAccepted.
    """

    @patch('asyncio.run')
    @patch('gittensor.cli.miner_commands.check._require_validator_axons')
    @patch('gittensor.cli.miner_commands.check._require_registered')
    @patch('gittensor.cli.miner_commands.check._connect_bittensor')
    def test_check_exits_non_zero_when_no_validator_has_valid_pat(
        self,
        connect,
        _require_reg,
        require_axons,
        async_run,
        runner,
    ):
        connect.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        require_axons.return_value = ([_fake_axon('hk1' * 16), _fake_axon('hk2' * 16)], [1, 2])
        async_run.return_value = [
            _fake_response(has_pat=False, pat_valid=False, rejection_reason=None),
            _fake_response(has_pat=None, pat_valid=None, rejection_reason=None),
        ]

        result = runner.invoke(
            cli,
            ['miner', 'check', '--json-output', '--wallet', 'test', '--hotkey', 'test'],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload['success'] is False
        assert payload['valid'] == 0

    @patch('asyncio.run')
    @patch('gittensor.cli.miner_commands.check._require_validator_axons')
    @patch('gittensor.cli.miner_commands.check._require_registered')
    @patch('gittensor.cli.miner_commands.check._connect_bittensor')
    def test_check_exits_zero_when_at_least_one_validator_has_valid_pat(
        self,
        connect,
        _require_reg,
        require_axons,
        async_run,
        runner,
    ):
        connect.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        require_axons.return_value = ([_fake_axon('hk1' * 16)], [1])
        async_run.return_value = [
            _fake_response(has_pat=True, pat_valid=True, rejection_reason=None),
        ]

        result = runner.invoke(
            cli,
            ['miner', 'check', '--json-output', '--wallet', 'test', '--hotkey', 'test'],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload['success'] is True
        assert payload['valid'] == 1


class TestPatCheckAggregateCounts:
    def test_splits_valid_no_pat_invalid_and_no_response(self):
        results = [
            {'pat_valid': True, 'has_pat': True},
            {'pat_valid': False, 'has_pat': False},
            {'pat_valid': False, 'has_pat': True},
            {'pat_valid': None, 'has_pat': None},
        ]
        assert _pat_check_aggregate_counts(results) == {
            'valid': 1,
            'no_pat': 1,
            'invalid_pat': 1,
            'no_response': 1,
        }
