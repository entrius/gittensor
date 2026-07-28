# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for `gitt validator weights` basket commands and weight quantization."""

import json
from unittest.mock import patch

from gittensor.cli.validator_commands.weights import quantize_weights
from tests.cli.registry_fakes import (
    RESOLVED,
    FakeRegistryClient,
    make_constants,
    make_packed,
    make_repo,
    make_subtensor,
    make_wallet,
)

WEIGHT_SUM = 65_535


def _patched_weights(client, wallet=None):
    wallet = wallet or make_wallet()
    return (
        patch('gittensor.cli.validator_commands.weights.resolve_repos_contract_and_network', return_value=RESOLVED),
        patch(
            'gittensor.cli.validator_commands.weights.make_registry_wallet_client',
            return_value=(wallet, make_subtensor(), client),
        ),
    )


class TestQuantizeWeights:
    def test_sum_is_exact(self):
        entries = quantize_weights({1: 60.0, 2: 40.0})
        assert entries == [(1, 39321), (2, 26214)]
        assert sum(w for _, w in entries) == WEIGHT_SUM

    def test_equal_thirds_sum_exact(self):
        entries = quantize_weights({1: 1.0, 2: 1.0, 3: 1.0})
        assert sum(w for _, w in entries) == WEIGHT_SUM
        assert max(w for _, w in entries) - min(w for _, w in entries) <= 1

    def test_deterministic_tie_break_by_id(self):
        assert quantize_weights({2: 1.0, 1: 1.0}) == quantize_weights({1: 1.0, 2: 1.0})

    def test_negligible_weight_dropped(self):
        entries = quantize_weights({1: 1e-9, 2: 1.0})
        assert entries == [(2, WEIGHT_SUM)]


class TestWeightsSet:
    def _client(self, **kwargs):
        return FakeRegistryClient(repos=[make_repo(1, 'entrius/gittensor'), make_repo(2, 'latent-to/btcli')], **kwargs)

    def test_set_normalizes_and_publishes_json(self, cli_root, runner):
        client = self._client()
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                ['validator', 'weights', 'set', 'entrius/gittensor=60', 'latent-to/btcli=40', '-y', '--json'],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['success'] is True
        assert [(e['github_id'], e['weight']) for e in payload['entries']] == [(1, 39321), (2, 26214)]
        assert client.calls == [('set_basket', [(1, 39321), (2, 26214)])]

    def test_set_accepts_numeric_ids(self, cli_root, runner):
        client = self._client()
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['v', 'w', 'set', '1=1', '-y', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert client.calls == [('set_basket', [(1, WEIGHT_SUM)])]

    def test_set_unregistered_repo_fails_validation(self, cli_root, runner):
        client = self._client()
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                ['validator', 'weights', 'set', 'nope/missing=1', '-y', '--json'],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['error']['type'] == 'bad_parameter'
        assert 'nope/missing' in payload['error']['message']
        assert client.calls == []

    def test_set_over_basket_cap_fails(self, cli_root, runner):
        repos = [make_repo(i, f'org/repo{i}') for i in range(1, 4)]
        client = FakeRegistryClient(repos=repos, packed=make_packed(make_constants(basket_cap=2)))
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(
                cli_root,
                ['validator', 'weights', 'set', '1=1', '2=1', '3=1', '-y', '--json'],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['error']['type'] == 'bad_parameter'
        assert 'capped at 2' in payload['error']['message']

    def test_set_zero_weight_rejected_before_connecting(self, cli_root, runner):
        result = runner.invoke(
            cli_root, ['validator', 'weights', 'set', 'entrius/gittensor=0', '--json'], catch_exceptions=False
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['error']['type'] == 'bad_parameter'
        assert 'positive' in payload['error']['message']

    def test_set_declined_confirmation_publishes_nothing(self, cli_root, runner):
        client = self._client()
        resolve, make = _patched_weights(client)
        with resolve, make, patch('gittensor.cli.core.helpers._is_interactive', return_value=True):
            result = runner.invoke(
                cli_root, ['validator', 'weights', 'set', '1=1'], input='n\n', catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert client.calls == []

    def test_set_rejected_publish_is_tx_failed(self, cli_root, runner):
        client = self._client(set_basket_result=False)
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(
                cli_root, ['validator', 'weights', 'set', '1=1', '-y', '--json'], catch_exceptions=False
            )

        assert result.exit_code != 0
        assert json.loads(result.stdout)['error']['type'] == 'tx_failed'


class TestWeightsShowClear:
    def test_show_json_own_and_all_baskets(self, cli_root, runner):
        client = FakeRegistryClient(
            repos=[make_repo(1, 'entrius/gittensor'), make_repo(2, 'latent-to/btcli')],
            baskets={
                '5FakeHotkey': [(1, WEIGHT_SUM)],
                '5OtherVali': [(1, 30000), (2, 35535)],
            },
        )
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['validator', 'weights', 'show', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['hotkey'] == '5FakeHotkey'
        assert payload['own_basket'] == [
            {'github_id': 1, 'repo': 'entrius/gittensor', 'weight': WEIGHT_SUM, 'share': 1.0}
        ]
        assert set(payload['baskets']) == {'5FakeHotkey', '5OtherVali'}

    def test_show_json_without_own_basket(self, cli_root, runner):
        client = FakeRegistryClient(repos=[make_repo(1, 'entrius/gittensor')])
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['validator', 'weights', 'show', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['own_basket'] is None

    def test_clear_confirmed_json(self, cli_root, runner):
        client = FakeRegistryClient()
        resolve, make = _patched_weights(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['validator', 'weights', 'clear', '-y', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {'success': True, 'cleared': True}
        assert client.calls == [('clear_basket',)]

    def test_clear_declined_confirmation_sends_nothing(self, cli_root, runner):
        client = FakeRegistryClient()
        resolve, make = _patched_weights(client)
        with resolve, make, patch('gittensor.cli.core.helpers._is_interactive', return_value=True):
            result = runner.invoke(cli_root, ['validator', 'weights', 'clear'], input='n\n', catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert client.calls == []
