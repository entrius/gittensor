# The MIT License (MIT)
# Copyright © 2025 Entrius

"""CLI tests for `issues register` wallet resolution."""

from unittest.mock import MagicMock, patch


def _register_mocks(load_config_return):
    """Patch everything the register command touches up to the wallet load."""
    return (
        patch(
            'gittensor.cli.issue_commands.mutations._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'finney'),
        ),
        patch('gittensor.cli.issue_commands.helpers.load_config', return_value=load_config_return),
        patch('gittensor.cli.issue_commands.mutations.validate_repository', return_value=('owner', 'repo')),
        patch('gittensor.cli.issue_commands.mutations.validate_github_issue', return_value={}),
        patch('bittensor.Subtensor', return_value=MagicMock()),
    )


def test_register_honors_explicit_wallet_default_over_config(cli_root, runner):
    """`gitt issues register --wallet default` must sign with the wallet the user
    named on the command line, even though it equals the option default and a
    different wallet is set in config.

    Regression: register resolved the wallet with a value comparison
    (`wallet_name != 'default'`) instead of Click's ParameterSource, so an
    explicit `--wallet default` was silently overridden by the config wallet —
    unlike `harvest`, which resolves via `resolve_wallet_config`. Since register
    spends real ALPHA and is owner-only, signing with the wrong wallet matters.
    """
    captured = {}

    class FakeWallet:
        def __init__(self, name=None, hotkey=None):
            captured['name'] = name
            captured['hotkey'] = hotkey
            self.coldkey = object()

    fake_client = MagicMock()
    fake_client.register_issue.return_value = ('0xhash', None)

    a, b, c, d, e = _register_mocks({'wallet': 'alice', 'hotkey': 'bob'})
    with (
        a,
        b,
        c,
        d,
        e,
        patch('bittensor.Wallet', FakeWallet),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        result = runner.invoke(
            cli_root,
            [
                'issues',
                'register',
                '--repo',
                'owner/repo',
                '--issue',
                '1',
                '--bounty',
                '10',
                '--wallet',
                'default',
                '-y',
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    # Explicit --wallet default wins over the config wallet 'alice'.
    assert captured['name'] == 'default'
    # --hotkey was not passed, so the config hotkey is still used.
    assert captured['hotkey'] == 'bob'


def test_register_falls_back_to_config_wallet_when_flag_omitted(cli_root, runner):
    """When neither --wallet nor --hotkey is passed, register uses the config
    wallet/hotkey (unchanged behavior, guards against over-correcting)."""
    captured = {}

    class FakeWallet:
        def __init__(self, name=None, hotkey=None):
            captured['name'] = name
            captured['hotkey'] = hotkey
            self.coldkey = object()

    fake_client = MagicMock()
    fake_client.register_issue.return_value = ('0xhash', None)

    a, b, c, d, e = _register_mocks({'wallet': 'alice', 'hotkey': 'bob'})
    with (
        a,
        b,
        c,
        d,
        e,
        patch('bittensor.Wallet', FakeWallet),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
    ):
        result = runner.invoke(
            cli_root,
            ['issues', 'register', '--repo', 'owner/repo', '--issue', '1', '--bounty', '10', '-y'],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    assert captured['name'] == 'alice'
    assert captured['hotkey'] == 'bob'
