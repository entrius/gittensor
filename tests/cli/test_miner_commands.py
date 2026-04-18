# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
import sys
import types
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


def _stub_miner_network(monkeypatch, *, response, synapse_name):
    fake_hotkey = types.SimpleNamespace(ss58_address='5FakeHotkeyFromStub')
    fake_wallet = types.SimpleNamespace(hotkey=fake_hotkey)
    fake_axon = types.SimpleNamespace(hotkey='5ValidatorHotkey123456', is_serving=True)
    fake_metagraph = types.SimpleNamespace(
        hotkeys=[fake_hotkey.ss58_address],
        n=1,
        axons=[fake_axon],
        validator_trust=[1.0],
    )

    class FakeSubtensor:
        def __init__(self, network=None):
            self.network = network

        def metagraph(self, netuid=None):
            return fake_metagraph

    class FakeDendrite:
        def __init__(self, wallet=None):
            self.wallet = wallet

    class FakeSynapse:
        def __init__(self, *args, **kwargs):
            pass

    fake_bt = types.SimpleNamespace(
        Wallet=lambda name=None, hotkey=None: fake_wallet,
        Subtensor=FakeSubtensor,
        Dendrite=FakeDendrite,
    )
    fake_synapses = types.SimpleNamespace(**{synapse_name: FakeSynapse})
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)
    monkeypatch.setitem(sys.modules, 'gittensor.synapses', fake_synapses)

    def fake_asyncio_run(coro):
        coro.close()
        return [response]

    return fake_axon, fake_asyncio_run


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

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=True)
    def test_json_mode_exits_nonzero_when_no_validators_accept_pat(self, mock_validate, runner, monkeypatch):
        fake_response = types.SimpleNamespace(
            accepted=False,
            rejection_reason='PAT rejected',
            dendrite=types.SimpleNamespace(status_code=200),
        )
        fake_axon, fake_asyncio_run = _stub_miner_network(
            monkeypatch,
            response=fake_response,
            synapse_name='PatBroadcastSynapse',
        )

        monkeypatch.setattr('gittensor.cli.miner_commands.post._get_validator_axons', lambda metagraph: ([fake_axon], [50]))
        monkeypatch.setattr('gittensor.cli.miner_commands.post.asyncio.run', fake_asyncio_run)

        result = runner.invoke(
            cli,
            ['miner', 'post', '--json-output', '--wallet', 'test', '--hotkey', 'test'],
            env={'GITTENSOR_MINER_PAT': 'ghp_test123'},
        )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output['success'] is False
        assert output['accepted'] == 0


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

    def test_json_mode_exits_nonzero_when_no_validators_have_valid_pat(self, runner, monkeypatch):
        fake_response = types.SimpleNamespace(has_pat=False, pat_valid=False, rejection_reason='No PAT stored')
        fake_axon, fake_asyncio_run = _stub_miner_network(
            monkeypatch,
            response=fake_response,
            synapse_name='PatCheckSynapse',
        )

        monkeypatch.setattr('gittensor.cli.miner_commands.check._get_validator_axons', lambda metagraph: ([fake_axon], [50]))
        monkeypatch.setattr('gittensor.cli.miner_commands.check.asyncio.run', fake_asyncio_run)

        result = runner.invoke(cli, ['miner', 'check', '--json-output', '--wallet', 'test', '--hotkey', 'test'])

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output['valid'] == 0
        assert output['invalid'] == 1
