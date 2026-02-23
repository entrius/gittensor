# The MIT License (MIT)
# Copyright © 2025 Entrius

"""CLI tests for `issues submissions` command."""

import json
from unittest.mock import patch

def test_submissions_json_schema_is_stable(cli_root, runner, sample_issue, sample_prs):
    with (
        patch('gittensor.cli.issue_commands.submissions.get_contract_address', return_value='0xabc'),
        patch('gittensor.cli.issue_commands.submissions.resolve_network', return_value=('ws://x', 'test')),
        patch('gittensor.cli.issue_commands.submissions.fetch_issue_from_contract', return_value=sample_issue),
        patch('gittensor.cli.issue_commands.submissions.get_github_pat', return_value='token'),
        patch('gittensor.cli.issue_commands.submissions.fetch_issue_prs', return_value=sample_prs),
    ):
        result = runner.invoke(
            cli_root,
            ['issues', 'submissions', '--id', '42', '--json'],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert '\x1b[' not in result.output

    payload = json.loads(result.output)
    assert set(payload.keys()) == {'issue_id', 'repository', 'issue_number', 'submission_count', 'submissions'}
    assert payload['issue_id'] == 42
    assert payload['repository'] == 'entrius/gittensor'
    assert payload['issue_number'] == 223
    assert payload['submission_count'] == 2
    assert isinstance(payload['submissions'], list)

    expected_keys = {
        'number',
        'title',
        'author',
        'state',
        'created_at',
        'merged_at',
        'url',
        'review_count',
        'closes_issue',
    }
    first = payload['submissions'][0]
    assert set(first.keys()) == expected_keys
    assert isinstance(first['closes_issue'], bool)
    assert first['closes_issue'] is True


def test_submissions_json_handles_missing_closing_numbers(
    cli_root, runner, sample_issue, sample_prs_missing_closing
):
    with (
        patch('gittensor.cli.issue_commands.submissions.get_contract_address', return_value='0xabc'),
        patch('gittensor.cli.issue_commands.submissions.resolve_network', return_value=('ws://x', 'test')),
        patch('gittensor.cli.issue_commands.submissions.fetch_issue_from_contract', return_value=sample_issue),
        patch('gittensor.cli.issue_commands.submissions.get_github_pat', return_value='token'),
        patch(
            'gittensor.cli.issue_commands.submissions.fetch_issue_prs',
            return_value=sample_prs_missing_closing,
        ),
    ):
        result = runner.invoke(
            cli_root,
            ['issues', 'submissions', '--id', '42', '--json'],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['submission_count'] == 1
    assert payload['submissions'][0]['closes_issue'] is False


def test_submissions_json_missing_contract_returns_config_error(cli_root, runner):
    with patch('gittensor.cli.issue_commands.submissions.get_contract_address', return_value=''):
        result = runner.invoke(
            cli_root,
            ['issues', 'submissions', '--id', '42', '--json'],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert payload['error']['type'] == 'config_error'
    assert 'Contract address not configured' in payload['error']['message']


def test_submissions_json_invalid_issue_id_returns_bad_parameter(cli_root, runner):
    for invalid_issue_id in [0, -1, 1_000_000]:
        result = runner.invoke(
            cli_root,
            ['issues', 'submissions', '--id', str(invalid_issue_id), '--json'],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload['success'] is False
        assert payload['error']['type'] == 'bad_parameter'


def test_submissions_human_no_open_prs_message(cli_root, runner, sample_issue):
    with (
        patch('gittensor.cli.issue_commands.submissions.get_contract_address', return_value='0xabc'),
        patch('gittensor.cli.issue_commands.submissions.resolve_network', return_value=('ws://x', 'test')),
        patch('gittensor.cli.issue_commands.submissions.fetch_issue_from_contract', return_value=sample_issue),
        patch('gittensor.cli.issue_commands.submissions.get_github_pat', return_value='token'),
        patch('gittensor.cli.issue_commands.submissions.fetch_issue_prs', return_value=[]),
    ):
        result = runner.invoke(
            cli_root,
            ['issues', 'submissions', '--id', '42'],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert 'No open PR submissions found.' in result.output
