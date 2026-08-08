# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for wallet resolution in `gitt issues register`.

The register path signs an owner-only on-chain transaction with the resolved
wallet's coldkey, so the wallet actually loaded must follow the documented
priority: explicit CLI flags first, then the config file, then the defaults.
The command previously used a bare `!= 'default'` comparison, which made an
explicit `--wallet-name default` indistinguishable from "no flag given" — the
config file's wallet silently replaced the wallet the user asked for by name.
`resolve_wallet_config` (already used by `gitt harvest`) is ParameterSource-
aware and resolves all three cases correctly.
"""

import json
import types
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.mutations import issue_register

_CONTRACT = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'


@pytest.fixture
def runner():
    return CliRunner()


def _invoke_register(runner, tmp_path, config, extra_args=()):
    """Run `issues register` with all network/GitHub/contract I/O stubbed out.

    ``config`` is written to a real temp config file and CONFIG_FILE is
    redirected at it, so resolution exercises the genuine load path. Returns
    (result, captured) where captured holds the (name, hotkey) pair passed to
    bt.Wallet — i.e. the wallet the command would actually sign with.
    """
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps(config))
    captured = {}

    class FakeWallet:
        def __init__(self, name=None, hotkey=None):
            captured['name'] = name
            captured['hotkey'] = hotkey
            self.coldkey = MagicMock()

    fake_bt = types.SimpleNamespace(Wallet=FakeWallet, Subtensor=MagicMock())
    fake_client = MagicMock()
    fake_client.register_issue.return_value = ('0xabc', None)

    with (
        patch.dict('sys.modules', {'bittensor': fake_bt}),
        patch('gittensor.cli.issue_commands.helpers.CONFIG_FILE', config_file),
        patch('gittensor.cli.issue_commands.mutations.validate_repository', return_value=('owner', 'repo')),
        patch('gittensor.cli.issue_commands.mutations.validate_github_issue', return_value={}),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        result = runner.invoke(
            issue_register,
            [
                '--repo',
                'owner/repo',
                '--issue',
                '1',
                '--bounty',
                '10',
                '--rpc-url',
                'wss://stub-endpoint:9944',
                '--contract',
                _CONTRACT,
                '--yes',
                *extra_args,
            ],
            catch_exceptions=False,
        )
    return result, captured


class TestRegisterWalletResolution:
    """CLI flags > config file > defaults — including flags spelled 'default'."""

    def test_explicit_default_flags_beat_config(self, runner, tmp_path):
        """A wallet literally named "default" must be selectable even when the
        config file names a different wallet."""
        result, captured = _invoke_register(
            runner,
            tmp_path,
            config={'wallet': 'treasury-cold', 'hotkey': 'tk'},
            extra_args=['--wallet-name', 'default', '--wallet-hotkey', 'default'],
        )

        assert result.exit_code == 0, result.output
        assert captured == {'name': 'default', 'hotkey': 'default'}

    def test_config_fills_in_when_no_flags_given(self, runner, tmp_path):
        result, captured = _invoke_register(
            runner,
            tmp_path,
            config={'wallet': 'treasury-cold', 'hotkey': 'tk'},
        )

        assert result.exit_code == 0, result.output
        assert captured == {'name': 'treasury-cold', 'hotkey': 'tk'}

    def test_explicit_named_wallet_beats_config(self, runner, tmp_path):
        result, captured = _invoke_register(
            runner,
            tmp_path,
            config={'wallet': 'treasury-cold', 'hotkey': 'tk'},
            extra_args=['--wallet-name', 'owner', '--wallet-hotkey', 'ok'],
        )

        assert result.exit_code == 0, result.output
        assert captured == {'name': 'owner', 'hotkey': 'ok'}

    def test_defaults_used_when_no_flags_and_no_config(self, runner, tmp_path):
        result, captured = _invoke_register(runner, tmp_path, config={})

        assert result.exit_code == 0, result.output
        assert captured == {'name': 'default', 'hotkey': 'default'}
