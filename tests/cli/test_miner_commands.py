# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor import __version__
from gittensor.cli.main import cli
from gittensor.cli.miner_commands.helpers import (
    _broadcast_pat_with_retry,
    _get_validator_axons,
    _pat_check_aggregate_counts,
    _pat_post_aggregate_counts,
    _pat_post_row_category,
    _require_validator_axons,
    _resolve_endpoint,
)
from gittensor.constants import NETWORK_MAP


def _fake_metagraph(rows: list[tuple[float, bool, float]]):
    """Build a metagraph stub from (vtrust, serving, stake) per UID."""
    n = len(rows)
    return SimpleNamespace(
        n=n,
        validator_trust=[vt for vt, _, _ in rows],
        S=[stake for _, _, stake in rows],
        axons=[SimpleNamespace(is_serving=serving, hotkey=f'5Hk{i:02d}') for i, (_, serving, _) in enumerate(rows)],
    )


@pytest.fixture
def runner():
    return CliRunner()


class TestMinerPost:
    @patch('gittensor.cli.miner_commands.post.click.prompt', return_value='ghp_fake')
    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=None)
    def test_no_pat_prompts_interactively(self, mock_validate, mock_prompt, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        runner.invoke(cli, ['miner', 'post', '--wallet', 'test', '--hotkey', 'test'])
        mock_prompt.assert_called_once_with('Enter your GitHub Personal Access Token', hide_input=True)

    def test_no_pat_json_mode_exits(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--json', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output['success'] is False

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=None)
    def test_pat_flag_used(self, mock_validate, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--pat', 'ghp_test123', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        assert 'invalid' in result.stderr.lower() or 'expired' in result.stderr.lower()
        mock_validate.assert_called_once_with('ghp_test123')

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=None)
    def test_invalid_pat_exits(self, mock_validate, runner, monkeypatch):
        monkeypatch.setenv('GITTENSOR_MINER_PAT', 'ghp_invalid')
        result = runner.invoke(cli, ['miner', 'post', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        assert 'invalid' in result.stderr.lower() or 'expired' in result.stderr.lower()

    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'post', '--help'])
        assert result.exit_code == 0
        assert 'Broadcast your GitHub PAT' in result.output

    def test_miner_alias(self, runner):
        """gitt m post should work as alias for gitt miner post."""
        result = runner.invoke(cli, ['m', 'post', '--help'])
        assert result.exit_code == 0
        assert 'Broadcast your GitHub PAT' in result.output

    def test_json_envelope_counts_sum_to_total_validators(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        metagraph = _fake_metagraph(
            [
                (0.9, True, 50_000.0),
                (0.8, True, 40_000.0),
                (0.7, True, 30_000.0),
            ]
        )
        metagraph.hotkeys = ['5MinerHotkey']
        wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address='5MinerHotkey'))
        resp_by_hotkey = {
            '5Hk00': SimpleNamespace(accepted=True, rejection_reason=None, dendrite=SimpleNamespace(status_code=200)),
            '5Hk01': SimpleNamespace(
                accepted=False, rejection_reason='denied', dendrite=SimpleNamespace(status_code=403)
            ),
            '5Hk02': SimpleNamespace(accepted=None, rejection_reason=None, dendrite=SimpleNamespace(status_code=None)),
        }

        class FakeDendrite:
            # Axon-aware so a no-response validator stays no-response across retries.
            async def __call__(self, *, axons, **kwargs):
                return [resp_by_hotkey[a.hotkey] for a in axons]

        with (
            patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value='testuser'),
            patch(
                'gittensor.cli.miner_commands.post._connect_bittensor',
                return_value=(wallet, object(), metagraph, FakeDendrite()),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    'miner',
                    'post',
                    '--json',
                    '--pat',
                    'ghp_test123',
                    '--wallet',
                    'test',
                    '--hotkey',
                    'test',
                ],
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.stdout)
        assert output['github_login'] == 'testuser'
        assert output['total_validators'] == 3
        assert output['accepted'] == 1
        assert output['rejected'] == 1
        assert output['no_response'] == 1
        assert output['accepted'] + output['rejected'] + output['no_response'] == output['total_validators']


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


class TestResolveEndpoint:
    """Unit tests for miner _resolve_endpoint config-fallback precedence.

    Precedence (high to low):
      1. --rpc-url flag
      2. --network flag
      3. recognized config `network`  ← must beat stale `ws_endpoint`
      4. config `ws_endpoint`
      5. default finney
    """

    def test_rpc_url_beats_everything(self):
        assert _resolve_endpoint(network=None, rpc_url='ws://custom:9944') == 'ws://custom:9944'

    def test_network_flag_beats_config(self):
        assert _resolve_endpoint(network='finney', rpc_url=None) == NETWORK_MAP['finney']

    def test_config_recognized_network_beats_stale_ws_endpoint(self, monkeypatch):
        """Regression: config network=finney must win over a stale ws_endpoint."""

        def fake_config(key):
            return {'network': 'finney', 'ws_endpoint': 'ws://127.0.0.1:9944'}.get(key)

        monkeypatch.setattr('gittensor.cli.miner_commands.helpers._load_config_value', fake_config)
        assert _resolve_endpoint(None, None) == NETWORK_MAP['finney']

    def test_config_test_network_beats_stale_ws_endpoint(self, monkeypatch):
        """Regression: config network=test must win over a stale ws_endpoint."""

        def fake_config(key):
            return {'network': 'test', 'ws_endpoint': 'ws://127.0.0.1:9944'}.get(key)

        monkeypatch.setattr('gittensor.cli.miner_commands.helpers._load_config_value', fake_config)
        assert _resolve_endpoint(None, None) == NETWORK_MAP['test']

    def test_unrecognized_config_network_falls_back_to_ws_endpoint(self, monkeypatch):
        """Unrecognized config network must not block ws_endpoint from being used."""

        def fake_config(key):
            return {'network': 'my_custom_chain', 'ws_endpoint': 'ws://mynode:9944'}.get(key)

        monkeypatch.setattr('gittensor.cli.miner_commands.helpers._load_config_value', fake_config)
        assert _resolve_endpoint(None, None) == 'ws://mynode:9944'

    def test_ws_endpoint_used_when_no_config_network(self, monkeypatch):
        """ws_endpoint alone is still honoured when no network is configured."""

        def fake_config(key):
            return {'ws_endpoint': 'ws://mynode:9944'}.get(key)

        monkeypatch.setattr('gittensor.cli.miner_commands.helpers._load_config_value', fake_config)
        assert _resolve_endpoint(None, None) == 'ws://mynode:9944'

    def test_default_finney_when_nothing_configured(self, monkeypatch):
        monkeypatch.setattr('gittensor.cli.miner_commands.helpers._load_config_value', lambda _: None)
        assert _resolve_endpoint(None, None) == NETWORK_MAP['finney']


class TestCliVersion:
    def test_version_matches_package_version(self, runner):
        result = runner.invoke(cli, ['--version'])
        assert result.exit_code == 0
        assert result.output == f'gittensor, version {__version__}\n'


class TestValidatorAxonFilter:
    def test_passes_when_all_thresholds_met(self):
        mg = _fake_metagraph([(0.9, True, 50_000.0)])
        axons, uids, excluded = _get_validator_axons(mg, min_vtrust=0.25, min_stake=15_000.0)
        assert uids == [0]
        assert len(axons) == 1
        assert excluded == []

    def test_silently_drops_below_vtrust(self):
        # Sub-vtrust UIDs are not validators — never surfaced in `excluded`.
        mg = _fake_metagraph([(0.1, True, 100_000.0)])
        axons, uids, excluded = _get_validator_axons(mg, min_vtrust=0.25, min_stake=15_000.0)
        assert uids == []
        assert axons == []
        assert excluded == []

    def test_excludes_when_not_serving(self):
        mg = _fake_metagraph([(0.99, False, 100_000.0)])
        _, uids, excluded = _get_validator_axons(mg, min_vtrust=0.25, min_stake=15_000.0)
        assert uids == []
        assert len(excluded) == 1
        assert excluded[0]['uid'] == 0
        assert excluded[0]['reasons'] == ['not serving an axon']

    def test_excludes_when_below_stake_threshold(self):
        mg = _fake_metagraph([(0.99, True, 1_630.0)])
        _, uids, excluded = _get_validator_axons(mg, min_vtrust=0.25, min_stake=15_000.0)
        assert uids == []
        assert len(excluded) == 1
        assert excluded[0]['uid'] == 0
        assert 'stake 1,630 α below 15,000 α threshold' in excluded[0]['reasons'][0]

    def test_combines_reasons_when_both_fail(self):
        mg = _fake_metagraph([(0.99, False, 1_000.0)])
        _, _, excluded = _get_validator_axons(mg, min_vtrust=0.25, min_stake=15_000.0)
        assert len(excluded[0]['reasons']) == 2


class TestRequireValidatorAxonsErrorPath:
    """Regression: the error path must surface the same `excluded` payload the
    success path renders, so operators see which threshold eliminated each UID."""

    def test_filtered_json_envelope_includes_skipped_array(self, capsys):
        mg = _fake_metagraph(
            [
                (0.99, True, 5_000.0),
                (0.85, True, 3_000.0),
                (0.72, True, 8_000.0),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            _require_validator_axons(mg, True, min_vtrust=0.25, min_stake=15_000.0)
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload['success'] is False
        assert payload['error']['type'] == 'no_validators_eligible'
        assert '--min-stake' in payload['error']['message']
        assert '--min-vtrust' in payload['error']['message']
        assert len(payload['skipped']) == 3
        assert {entry['uid'] for entry in payload['skipped']} == {0, 1, 2}
        assert all(entry['reasons'] for entry in payload['skipped'])

    def test_filtered_tty_renders_skipped_table_and_error(self, capsys):
        mg = _fake_metagraph(
            [
                (0.99, True, 5_000.0),
                (0.85, False, 50_000.0),
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            _require_validator_axons(mg, False, min_vtrust=0.25, min_stake=15_000.0)
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert 'Skipped Validators' in out
        assert 'No validators passed' in out
        assert 'Error:' in out

    def test_truly_empty_metagraph_keeps_generic_message(self, capsys):
        mg = _fake_metagraph([])
        with pytest.raises(SystemExit) as exc_info:
            _require_validator_axons(mg, True, min_vtrust=0.25, min_stake=15_000.0)
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload == {
            'success': False,
            'error': {
                'type': 'cli_error',
                'message': 'No reachable validator axons found on the network.',
            },
        }

    def test_subvtrust_only_metagraph_keeps_generic_message(self, capsys):
        # Sub-vtrust UIDs are dropped silently and never enter `excluded`,
        # so the message should remain the generic one — not the threshold one.
        mg = _fake_metagraph([(0.10, True, 50_000.0), (0.05, True, 100_000.0)])
        with pytest.raises(SystemExit) as exc_info:
            _require_validator_axons(mg, True, min_vtrust=0.25, min_stake=15_000.0)
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload['error']['type'] == 'cli_error'
        assert payload['error']['message'] == 'No reachable validator axons found on the network.'
        assert 'skipped' not in payload


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
            'inconclusive': 0,
            'no_response': 1,
        }


class TestPatPostRowCategory:
    def test_accepted_true_returns_accepted(self):
        assert _pat_post_row_category({'accepted': True}) == 'accepted'

    def test_accepted_false_returns_rejected(self):
        assert _pat_post_row_category({'accepted': False}) == 'rejected'

    def test_accepted_none_returns_no_response(self):
        assert _pat_post_row_category({'accepted': None}) == 'no_response'

    def test_missing_accepted_key_returns_no_response(self):
        assert _pat_post_row_category({}) == 'no_response'


class TestPatPostAggregateCounts:
    def test_splits_accepted_rejected_and_no_response(self):
        results = [
            {'accepted': True},
            {'accepted': True},
            {'accepted': False},
            {'accepted': None},
            {'accepted': None},
        ]
        assert _pat_post_aggregate_counts(results) == {
            'accepted': 2,
            'rejected': 1,
            'no_response': 2,
        }

    def test_empty_results_returns_zero_counts(self):
        assert _pat_post_aggregate_counts([]) == {
            'accepted': 0,
            'rejected': 0,
            'no_response': 0,
        }

    def test_no_response_is_not_collapsed_into_rejected(self):
        """Regression: JSON output previously reported `rejected = total - accepted`,
        silently bucketing no_response into rejected. Counts must stay distinct."""
        results = [{'accepted': False}, {'accepted': None}]
        counts = _pat_post_aggregate_counts(results)
        assert counts['rejected'] == 1
        assert counts['no_response'] == 1


def _post_resp(accepted, reason=None, status=None):
    return SimpleNamespace(accepted=accepted, rejection_reason=reason, dendrite=SimpleNamespace(status_code=status))


class TestBroadcastWithRetry:
    """`_broadcast_pat_with_retry` retries ONLY no-response validators, so a transient
    blip during one broadcast is not a silent, permanent coverage gap."""

    def test_retries_only_the_no_response_validator(self):
        axons = [SimpleNamespace(hotkey='5Hk00accepts'), SimpleNamespace(hotkey='5Hk01flaky')]
        uids = [10, 20]
        calls = []

        class FakeDendrite:
            def __init__(self):
                self.n = 0

            async def __call__(self, *, axons, **kw):
                calls.append([a.hotkey for a in axons])
                self.n += 1
                if self.n == 1:
                    return [_post_resp(True, status=200), _post_resp(None)]  # uid20: no response
                return [_post_resp(True, status=200)]  # uid20 accepts on retry

        results = _broadcast_pat_with_retry(FakeDendrite(), axons, uids, 'ghp_x', retries=2, delay=0)
        assert len(calls) == 2  # the no-response validator was retried
        assert calls[1] == ['5Hk01flaky']  # ONLY the no-response one was retried
        by_uid = {r['uid']: r for r in results}
        assert by_uid[10]['accepted'] is True
        assert by_uid[20]['accepted'] is True  # eventually delivered

    def test_explicit_rejection_is_final(self):
        axons = [SimpleNamespace(hotkey='5Hk00rejects')]
        calls = []

        class FakeDendrite:
            async def __call__(self, *, axons, **kw):
                calls.append(1)
                return [_post_resp(False, reason='denied', status=403)]

        results = _broadcast_pat_with_retry(FakeDendrite(), axons, [10], 'ghp_x', retries=3, delay=0)
        assert len(calls) == 1  # a rejection is not retried
        assert results[0]['accepted'] is False

    def test_persistent_no_response_is_surfaced_not_hidden(self):
        axons = [SimpleNamespace(hotkey='5Hk00down')]
        calls = []

        class FakeDendrite:
            async def __call__(self, *, axons, **kw):
                calls.append(1)
                return [_post_resp(None)]

        results = _broadcast_pat_with_retry(FakeDendrite(), axons, [10], 'ghp_x', retries=2, delay=0)
        assert len(calls) == 3  # initial attempt + 2 retries
        assert results[0]['accepted'] is None  # still uncovered — reported, not silently dropped


class TestMinerEnsure:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'ensure', '--help'])
        assert result.exit_code == 0
        assert 'missing' in result.output.lower()

    def test_ensure_alias(self, runner):
        result = runner.invoke(cli, ['m', 'ensure', '--help'])
        assert result.exit_code == 0

    def test_reposts_only_to_validators_missing_pat(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        # 3 validators: uid0 valid, uid1 missing (no_pat), uid2 valid.
        metagraph = _fake_metagraph([(0.9, True, 50_000.0), (0.8, True, 40_000.0), (0.7, True, 30_000.0)])
        metagraph.hotkeys = ['5MinerHotkey']
        wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address='5MinerHotkey'))
        broadcast_targets = []

        def check_resp(has_pat, pat_valid):
            return SimpleNamespace(has_pat=has_pat, pat_valid=pat_valid, rejection_reason=None)

        class FakeDendrite:
            async def __call__(self, *, axons, synapse, **kw):
                if type(synapse).__name__ == 'PatCheckSynapse':
                    by = {
                        '5Hk00': check_resp(True, True),
                        '5Hk01': check_resp(False, False),
                        '5Hk02': check_resp(True, True),
                    }
                    return [by[a.hotkey] for a in axons]
                broadcast_targets.append([a.hotkey for a in axons])
                return [_post_resp(True, status=200) for _ in axons]

        with (
            patch('gittensor.cli.miner_commands.ensure._validate_pat_locally', return_value='testuser'),
            patch(
                'gittensor.cli.miner_commands.ensure._connect_bittensor',
                return_value=(wallet, object(), metagraph, FakeDendrite()),
            ),
        ):
            result = runner.invoke(
                cli, ['miner', 'ensure', '--json', '--pat', 'ghp_x', '--wallet', 't', '--hotkey', 't']
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['success'] is True
        assert payload['total_validators'] == 3
        assert payload['already_valid'] == 2
        assert payload['reposted'] == 1
        assert payload['now_valid'] == 3
        assert payload['still_missing'] == []
        # Re-broadcast went ONLY to the missing validator, never to the two that had it.
        assert broadcast_targets == [['5Hk01']]

    def test_exits_nonzero_when_a_validator_stays_uncovered(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        metagraph = _fake_metagraph([(0.9, True, 50_000.0)])
        metagraph.hotkeys = ['5MinerHotkey']
        wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address='5MinerHotkey'))

        class FakeDendrite:
            async def __call__(self, *, axons, synapse, **kw):
                if type(synapse).__name__ == 'PatCheckSynapse':
                    return [SimpleNamespace(has_pat=False, pat_valid=False, rejection_reason=None) for _ in axons]
                return [_post_resp(None) for _ in axons]  # unreachable for the re-broadcast too

        with (
            patch('gittensor.cli.miner_commands.ensure._validate_pat_locally', return_value='testuser'),
            patch(
                'gittensor.cli.miner_commands.ensure._connect_bittensor',
                return_value=(wallet, object(), metagraph, FakeDendrite()),
            ),
        ):
            result = runner.invoke(
                cli,
                ['miner', 'ensure', '--json', '--pat', 'ghp_x', '--wallet', 't', '--hotkey', 't', '--retries', '1'],
            )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload['success'] is False
        assert payload['still_missing'] == [0]

    def test_watch_loops_and_resyncs_metagraph(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        metagraph = _fake_metagraph([(0.9, True, 50_000.0)])
        metagraph.hotkeys = ['5MinerHotkey']
        wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address='5MinerHotkey'))
        probes = []

        class FakeSubtensor:
            def __init__(self):
                self.metagraph_calls = 0

            def metagraph(self, netuid=None):
                self.metagraph_calls += 1
                return metagraph

        subtensor = FakeSubtensor()

        class FakeDendrite:
            async def __call__(self, *, axons, synapse, **kw):
                if type(synapse).__name__ == 'PatCheckSynapse':
                    probes.append(1)
                    return [SimpleNamespace(has_pat=True, pat_valid=True, rejection_reason=None) for _ in axons]
                return [_post_resp(True, status=200) for _ in axons]

        class FakeSleep:
            def __init__(self):
                self.n = 0

            def __call__(self, _seconds):
                self.n += 1
                if self.n >= 2:  # let two cycles run, then stop like Ctrl-C
                    raise KeyboardInterrupt

        with (
            patch('gittensor.cli.miner_commands.ensure._validate_pat_locally', return_value='testuser'),
            patch(
                'gittensor.cli.miner_commands.ensure._connect_bittensor',
                return_value=(wallet, subtensor, metagraph, FakeDendrite()),
            ),
            patch('gittensor.cli.miner_commands.ensure.time.sleep', FakeSleep()),
        ):
            result = runner.invoke(
                cli, ['miner', 'ensure', '--json', '--watch', '30', '--pat', 'ghp_x', '--wallet', 't', '--hotkey', 't']
            )
        assert result.exit_code == 0  # Ctrl-C / KeyboardInterrupt exits cleanly
        assert len(probes) == 2  # ran two coverage cycles
        assert subtensor.metagraph_calls == 1  # re-synced the metagraph between cycles
