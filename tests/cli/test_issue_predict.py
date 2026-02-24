# The MIT License (MIT)
# Copyright © 2025 Entrius

"""CLI tests for `issues predict` command."""

import json
from unittest.mock import patch


def test_predict_interactive_continue_cancel_skips_miner_validation(cli_root, runner, sample_issue, sample_prs):
    with (
        patch('gittensor.cli.issue_commands.predict.get_contract_address', return_value='0xabc'),
        patch('gittensor.cli.issue_commands.predict.resolve_network', return_value=('ws://x', 'test')),
        patch('gittensor.cli.issue_commands.predict.fetch_issue_from_contract', return_value=sample_issue),
        patch('gittensor.cli.issue_commands.predict.fetch_open_issue_pull_requests', return_value=sample_prs),
        patch('gittensor.cli.issue_commands.predict._is_interactive', return_value=True),
        patch('gittensor.cli.issue_commands.predict._resolve_registered_miner_hotkey') as mock_resolve_miner,
    ):
        result = runner.invoke(
            cli_root,
            ['issues', 'predict', '--id', '42'],
            input='n\n',
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert 'Prediction cancelled' in result.output
    mock_resolve_miner.assert_not_called()


def test_predict_json_success_payload_schema(cli_root, runner, sample_issue, sample_prs):
    with (
        patch('gittensor.cli.issue_commands.predict.get_contract_address', return_value='0xabc'),
        patch('gittensor.cli.issue_commands.predict.resolve_network', return_value=('ws://x', 'test')),
        patch('gittensor.cli.issue_commands.predict.fetch_issue_from_contract', return_value=sample_issue),
        patch('gittensor.cli.issue_commands.predict.fetch_open_issue_pull_requests', return_value=sample_prs),
        patch(
            'gittensor.cli.issue_commands.predict._resolve_registered_miner_hotkey',
            return_value='5FakeHotkey123',
        ),
        patch('gittensor.cli.issue_commands.predict.broadcast_predictions_stub') as mock_broadcast_stub,
    ):
        mock_broadcast_stub.side_effect = lambda payload: payload
        result = runner.invoke(
            cli_root,
            ['issues', 'predict', '--id', '42', '--pr', '101', '--probability', '0.7', '--json'],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    mock_broadcast_stub.assert_called_once()
    payload = json.loads(result.output)
    assert set(payload.keys()) == {'issue_id', 'repository', 'predictions'}
    assert payload['issue_id'] == 42
    assert payload['repository'] == 'entrius/gittensor'
    assert {int(k): v for k, v in payload['predictions'].items()} == {101: 0.7}


def test_predict_json_requires_non_interactive_inputs(runner, cli_root):
    result = runner.invoke(
        cli_root,
        ['issues', 'predict', '--id', '42', '--json'],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert '--json mode requires --pr/--probability or --json-input.' in payload['error']['message']


def test_predict_rejects_probability_out_of_range(runner, cli_root):
    result = runner.invoke(
        cli_root,
        ['issues', 'predict', '--id', '42', '--pr', '101', '--probability', '1.1', '--json'],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert 'Probability must be between 0.0 and 1.0' in payload['error']['message']


def test_predict_rejects_invalid_json_input(runner, cli_root):
    result = runner.invoke(
        cli_root,
        ['issues', 'predict', '--id', '42', '--json-input', '{bad json}', '--json'],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert 'Invalid JSON' in payload['error']['message']


def test_predict_rejects_json_input_probability_total_over_one(runner, cli_root):
    result = runner.invoke(
        cli_root,
        ['issues', 'predict', '--id', '42', '--json-input', '{"101": 0.8, "103": 0.3}', '--json'],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert 'Sum of probabilities must be <= 1.0' in payload['error']['message']


def test_predict_rejects_pr_not_in_open_set_before_miner_validation(cli_root, runner, sample_issue, sample_prs):
    with (
        patch('gittensor.cli.issue_commands.predict.get_contract_address', return_value='0xabc'),
        patch('gittensor.cli.issue_commands.predict.resolve_network', return_value=('ws://x', 'test')),
        patch('gittensor.cli.issue_commands.predict.fetch_issue_from_contract', return_value=sample_issue),
        patch('gittensor.cli.issue_commands.predict.fetch_open_issue_pull_requests', return_value=sample_prs),
        patch('gittensor.cli.issue_commands.predict._resolve_registered_miner_hotkey') as mock_resolve_miner,
    ):
        result = runner.invoke(
            cli_root,
            ['issues', 'predict', '--id', '42', '--pr', '999', '--probability', '0.2', '--json'],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert 'is not an open PR for this issue' in payload['error']['message']
    mock_resolve_miner.assert_not_called()


def test_predict_invalid_issue_id_returns_bad_parameter(runner, cli_root):
    for invalid_issue_id in [0, -1, 1_000_000]:
        result = runner.invoke(
            cli_root,
            ['issues', 'predict', '--id', str(invalid_issue_id), '--json'],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload['success'] is False
        assert payload['error']['type'] == 'bad_parameter'
