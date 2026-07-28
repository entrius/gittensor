# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for `gitt repo list/show/price` reads and JSON envelopes."""

import json
from unittest.mock import patch

from tests.cli.registry_fakes import (
    RESOLVED,
    FakeRegistryClient,
    make_constants,
    make_packed,
    make_repo,
    make_subtensor,
)


def _patched_view(client, block=100):
    return (
        patch('gittensor.cli.repo_commands.view.resolve_repos_contract_and_network', return_value=RESOLVED),
        patch('gittensor.cli.repo_commands.view.make_registry_client', return_value=(make_subtensor(block), client)),
    )


class TestRepoList:
    def test_list_json_includes_immunity_and_active(self, cli_root, runner):
        client = FakeRegistryClient(
            repos=[make_repo(1, 'a/old', reg_block=10), make_repo(2, 'b/new', reg_block=90)],
            packed=make_packed(make_constants(immunity_period=50)),
        )
        resolve, make = _patched_view(client, block=100)
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'list', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['success'] is True
        assert payload['count'] == 2
        assert payload['max_repos'] == 32
        old, new = payload['repos']
        assert old['full_name'] == 'a/old' and old['immune'] is False and old['immune_until'] == 60
        assert new['full_name'] == 'b/new' and new['immune'] is True and new['active'] is True

    def test_list_registry_read_failure_is_read_failed(self, cli_root, runner):
        client = FakeRegistryClient(packed=None)
        resolve, make = _patched_view(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'list', '--json'], catch_exceptions=False)

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['success'] is False
        assert payload['error']['type'] == 'read_failed'

    def test_repo_alias_r(self, cli_root, runner):
        client = FakeRegistryClient()
        resolve, make = _patched_view(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['r', 'list', '--json'], catch_exceptions=False)
        assert result.exit_code == 0, result.output


class TestRepoShow:
    def _client(self):
        return FakeRegistryClient(
            repos=[make_repo(42, 'entrius/gittensor')],
            params={42: {4: 100_000, 17: 30}},
            labels={42: {'bug': 1_500_000}},
            patterns={42: ['release/*']},
        )

    def test_show_by_name_json(self, cli_root, runner):
        resolve, make = _patched_view(self._client())
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'show', 'Entrius/Gittensor', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['repo']['github_id'] == 42
        assert payload['params'] == {'maintainer_cut': 100_000, 'pr_lookback_days': 30}
        assert payload['label_multipliers'] == {'bug': 1_500_000}
        assert payload['branch_patterns'] == ['release/*']

    def test_show_by_numeric_id(self, cli_root, runner):
        resolve, make = _patched_view(self._client())
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'show', '42', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['repo']['full_name'] == 'entrius/gittensor'

    def test_show_unknown_ref_fails(self, cli_root, runner):
        resolve, make = _patched_view(self._client())
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'show', 'nope/missing', '--json'], catch_exceptions=False)

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['success'] is False
        assert 'not found' in payload['error']['message']


class TestRepoPrice:
    def test_price_json(self, cli_root, runner):
        client = FakeRegistryClient(quote=500_000_000_000)
        resolve, make = _patched_view(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'price', '--json'], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload == {'success': True, 'price_raw': 500_000_000_000, 'price_alpha': '500.0000'}

    def test_price_unavailable_is_read_failed(self, cli_root, runner):
        client = FakeRegistryClient(quote=None)
        resolve, make = _patched_view(client)
        with resolve, make:
            result = runner.invoke(cli_root, ['repo', 'price', '--json'], catch_exceptions=False)

        assert result.exit_code != 0
        assert json.loads(result.stdout)['error']['type'] == 'read_failed'
