# The MIT License (MIT)
# Copyright © 2025 Entrius

"""_make_contract_client and harvest must resolve wallet/hotkey from config
when explicit CLI flags are absent (issue #1424)."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch


def _config_patch(wallet: str, hotkey: str):
    """Return a patch that injects a config with the given wallet/hotkey."""
    return patch(
        'gittensor.cli.issue_commands.helpers.load_config',
        return_value={'wallet': wallet, 'hotkey': hotkey},
    )


# ---------------------------------------------------------------------------
# _make_contract_client
# ---------------------------------------------------------------------------


def test_make_contract_client_uses_config_wallet_when_cli_is_default(cli_root, runner):
    """vote/admin commands should use config wallet when --wallet-name is not given."""
    captured = {}

    def _fake_make(contract_addr, ws_endpoint, wallet_name, wallet_hotkey):
        captured['wallet_name'] = wallet_name
        captured['wallet_hotkey'] = wallet_hotkey
        fake_wallet = MagicMock()
        fake_wallet.hotkey.ss58_address = '5Fake'
        fake_client = MagicMock()
        fake_client.cancel_vote.return_value = ('0xhash', None)
        return fake_wallet, fake_client

    with (
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        _config_patch('configured-wallet', 'configured-hotkey'),
        patch('gittensor.cli.issue_commands.vote._make_contract_client', side_effect=_fake_make),
    ):
        runner.invoke(cli_root, ['vote', 'cancel', '1', 'dup', '--yes'])

    assert captured.get('wallet_name') == 'default', \
        'helpers._make_contract_client receives the raw Click default; resolution happens inside it'


def test_make_contract_client_internal_resolves_config(tmp_path):
    """_make_contract_client itself must resolve 'default' wallet to config wallet."""
    from gittensor.cli.issue_commands.helpers import _make_contract_client

    fake_bt_wallet = MagicMock()
    fake_subtensor = MagicMock()
    fake_client = MagicMock()

    with (
        patch('gittensor.cli.issue_commands.helpers.load_config',
              return_value={'wallet': 'my-wallet', 'hotkey': 'my-hotkey'}),
        patch('bittensor.Wallet', return_value=fake_bt_wallet) as wallet_cls,
        patch('bittensor.Subtensor', return_value=fake_subtensor),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        _make_contract_client('5Addr', 'ws://x', 'default', 'default')

    wallet_cls.assert_called_once_with(name='my-wallet', hotkey='my-hotkey')


def test_make_contract_client_explicit_flag_wins_over_config():
    """Explicit CLI wallet flag must override the config value."""
    from gittensor.cli.issue_commands.helpers import _make_contract_client

    fake_bt_wallet = MagicMock()
    fake_subtensor = MagicMock()
    fake_client = MagicMock()

    with (
        patch('gittensor.cli.issue_commands.helpers.load_config',
              return_value={'wallet': 'config-wallet', 'hotkey': 'config-hotkey'}),
        patch('bittensor.Wallet', return_value=fake_bt_wallet) as wallet_cls,
        patch('bittensor.Subtensor', return_value=fake_subtensor),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        _make_contract_client('5Addr', 'ws://x', 'explicit-wallet', 'explicit-hotkey')

    wallet_cls.assert_called_once_with(name='explicit-wallet', hotkey='explicit-hotkey')


# ---------------------------------------------------------------------------
# harvest
# ---------------------------------------------------------------------------


def test_harvest_uses_config_wallet_when_flags_absent(cli_root, runner):
    """gitt harvest with no wallet flags should use config wallet/hotkey."""
    fake_wallet = MagicMock()
    fake_wallet.hotkey.ss58_address = '5Fake'
    fake_client = MagicMock()
    fake_client.harvest_emissions.return_value = {'tx_hash': '0xabc', 'success': True}

    with (
        patch(
            'gittensor.cli.issue_commands.mutations._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.mutations.load_config',
              return_value={'wallet': 'config-wallet', 'hotkey': 'config-hotkey'}),
        patch('bittensor.Wallet', return_value=fake_wallet) as wallet_cls,
        patch('bittensor.Subtensor', return_value=MagicMock()),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        runner.invoke(cli_root, ['harvest'], catch_exceptions=False)

    wallet_cls.assert_called_once_with(name='config-wallet', hotkey='config-hotkey')


def test_harvest_explicit_flag_wins_over_config(cli_root, runner):
    """gitt harvest --wallet-name explicit must override config."""
    fake_wallet = MagicMock()
    fake_wallet.hotkey.ss58_address = '5Fake'
    fake_client = MagicMock()
    fake_client.harvest_emissions.return_value = {'tx_hash': '0xabc', 'success': True}

    with (
        patch(
            'gittensor.cli.issue_commands.mutations._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.mutations.load_config',
              return_value={'wallet': 'config-wallet', 'hotkey': 'config-hotkey'}),
        patch('bittensor.Wallet', return_value=fake_wallet) as wallet_cls,
        patch('bittensor.Subtensor', return_value=MagicMock()),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        runner.invoke(
            cli_root,
            ['harvest', '--wallet-name', 'explicit-wallet', '--wallet-hotkey', 'explicit-hotkey'],
            catch_exceptions=False,
        )

    wallet_cls.assert_called_once_with(name='explicit-wallet', hotkey='explicit-hotkey')
