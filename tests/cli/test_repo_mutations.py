# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for `gitt repo` mutations: register resolution, set-params mapping and
bounds, and confirmation gating."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from tests.cli.registry_fakes import (
    RESOLVED,
    FakeRegistryClient,
    make_repo,
    make_subtensor,
    make_wallet,
)


def _patched_mutations(client, wallet=None):
    wallet = wallet or make_wallet()
    return (
        patch('gittensor.cli.repo_commands.mutations.resolve_repos_contract_and_network', return_value=RESOLVED),
        patch(
            'gittensor.cli.repo_commands.mutations.make_registry_wallet_client',
            return_value=(wallet, make_subtensor(), client),
        ),
    )


class TestRepoRegister:
    def test_register_with_id_override_json(self, cli_root, runner):
        client = FakeRegistryClient(quote=1_000_000_000)
        resolve, make = _patched_mutations(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                ['repo', 'register', 'Entrius/Gittensor', '--id', '42', '-y', '--json'],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['success'] is True
        assert payload['tx_hash'] == '0xdeadbeef'
        assert payload['github_id'] == 42
        assert client.calls == [('register', 42, 'entrius/gittensor', '5FakeHotkey')]

    def test_register_resolves_id_via_github(self, cli_root, runner):
        client = FakeRegistryClient()
        github = SimpleNamespace(status_code=200, ok=True, json=lambda: {'id': 777})
        resolve, make = _patched_mutations(client)
        with resolve, make, patch('gittensor.cli.repo_commands.helpers.requests.get', return_value=github):
            result = runner.invoke(
                cli_root, ['repo', 'register', 'entrius/gittensor', '-y', '--json'], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert client.calls[0][:2] == ('register', 777)

    def test_register_github_404_aborts(self, cli_root, runner):
        github = SimpleNamespace(status_code=404, ok=False, json=lambda: {})
        with patch('gittensor.cli.repo_commands.helpers.requests.get', return_value=github):
            result = runner.invoke(
                cli_root, ['repo', 'register', 'nope/missing', '-y', '--json'], catch_exceptions=False
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['success'] is False
        assert 'not found on GitHub' in payload['error']['message']

    def test_register_declined_confirmation_sends_nothing(self, cli_root, runner):
        client = FakeRegistryClient()
        resolve, make = _patched_mutations(client)
        with resolve, make, patch('gittensor.cli.core.helpers._is_interactive', return_value=True):
            result = runner.invoke(
                cli_root, ['repo', 'register', 'a/b', '--id', '1'], input='n\n', catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert client.calls == []

    def test_register_revert_is_tx_failed(self, cli_root, runner):
        client = FakeRegistryClient(tx_result=('0xbad', 'register failed: Module error'))
        resolve, make = _patched_mutations(client)
        with resolve, make:
            result = runner.invoke(
                cli_root, ['repo', 'register', 'a/b', '--id', '1', '-y', '--json'], catch_exceptions=False
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['error']['type'] == 'tx_failed'
        assert payload['tx_hash'] == '0xbad'


class TestRepoSetParams:
    def _client(self, **kwargs):
        return FakeRegistryClient(repos=[make_repo(42, 'entrius/gittensor')], **kwargs)

    def test_set_params_maps_names_and_scales_fp6(self, cli_root, runner):
        client = self._client()
        resolve, make = _patched_mutations(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                [
                    'repo',
                    'set-params',
                    'entrius/gittensor',
                    'maintainer_cut=0.1',
                    'pr_lookback_days=30',
                    '-y',
                    '--json',
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['success'] is True
        assert client.calls == [('set_param', 42, 4, 100_000), ('set_param', 42, 17, 30)]

    def test_set_params_unknown_name_lists_table(self, cli_root, runner):
        payload = _invoke_json_error(cli_root, runner, ['repo', 'set-params', '42', 'bogus_param=1', '-y', '--json'])
        assert payload['error']['type'] == 'bad_parameter'
        assert 'maintainer_cut' in payload['error']['message']

    def test_set_params_bounds_error_shows_bounds(self, cli_root, runner):
        client = self._client(bounds={4: (0, 200_000)})
        resolve, make = _patched_mutations(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                ['repo', 'set-params', '42', 'maintainer_cut=0.5', '-y', '--json'],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['error']['type'] == 'bad_parameter'
        assert '[0, 0.2]' in payload['error']['message']
        assert client.calls == []

    def test_set_params_integer_param_rejects_decimal(self, cli_root, runner):
        payload = _invoke_json_error(
            cli_root, runner, ['repo', 'set-params', '42', 'pr_lookback_days=1.5', '-y', '--json']
        )
        assert payload['error']['type'] == 'bad_parameter'
        assert 'integer' in payload['error']['message']

    def test_set_params_stops_at_first_failure(self, cli_root, runner):
        client = self._client(tx_result=(None, 'boom'))
        resolve, make = _patched_mutations(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                ['repo', 'set-params', '42', 'maintainer_cut=0.1', 'pr_lookback_days=30', '-y', '--json'],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['success'] is False
        assert len(payload['results']) == 1
        assert len(client.calls) == 1


class TestRepoDeregister:
    def test_deregister_declined_confirmation_sends_nothing(self, cli_root, runner):
        client = FakeRegistryClient(repos=[make_repo(42, 'entrius/gittensor')])
        resolve, make = _patched_mutations(client)
        with resolve, make, patch('gittensor.cli.core.helpers._is_interactive', return_value=True):
            result = runner.invoke(cli_root, ['repo', 'deregister', '42'], input='n\n', catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert client.calls == []

    def test_deregister_confirmed_json(self, cli_root, runner):
        client = FakeRegistryClient(repos=[make_repo(42, 'entrius/gittensor')])
        resolve, make = _patched_mutations(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'deregister', '42', '-y', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['success'] is True
        assert client.calls == [('deregister', 42)]


def _invoke_json_error(cli_root, runner, args):
    """Invoke expecting a JSON error envelope; returns the parsed payload."""
    result = runner.invoke(cli_root, args, catch_exceptions=False)
    assert result.exit_code != 0
    return json.loads(result.stdout)
