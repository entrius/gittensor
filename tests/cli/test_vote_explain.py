import json
from unittest.mock import Mock, patch

from gittensor.validator.issue_competitions.contract_client import ContractIssue, IssueStatus


def _issue() -> ContractIssue:
    return ContractIssue(
        id=7,
        github_url_hash=b'0' * 32,
        repository_full_name='entrius/gittensor',
        issue_number=12,
        bounty_amount=1_000_000_000,
        target_bounty=1_000_000_000,
        status=IssueStatus.ACTIVE,
        registered_at_block=1,
        is_fully_funded=True,
    )


def test_vote_explain_json_reports_dry_run_solution(cli_root, runner, tmp_path):
    pat_file = tmp_path / 'miner_pats.json'
    pat_file.write_text(json.dumps([{'github_id': '999', 'hotkey': 'hk999', 'pat': 'redacted'}]))

    client = Mock()
    client.get_issues_by_status.return_value = [_issue()]

    with (
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Contract', 'wss://test.finney.opentensor.ai:443', 'test'),
        ),
        patch('bittensor.Subtensor') as mock_subtensor,
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient', return_value=client
        ),
        patch(
            'gittensor.utils.github_api_tools.check_github_issue_closed',
            return_value={
                'is_closed': True,
                'solver_github_id': '999',
                'pr_number': 42,
                'solver_lookup_failed': False,
            },
        ),
        patch('gittensor.validator.utils.issue_competitions.get_miner_coldkey', return_value='ck999'),
    ):
        result = runner.invoke(
            cli_root,
            [
                'vote',
                'explain',
                '--json',
                '--github-token',
                'ghp_validator',
                '--pat-file',
                str(pat_file),
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['success'] is True
    assert payload['dry_run'] is True
    assert payload['registered_miner_count'] == 1
    assert payload['decisions'][0]['action'] == 'vote_solution'
    assert payload['decisions'][0]['solver_hotkey'] == 'hk999'
    assert payload['decisions'][0]['solver_coldkey'] == 'ck999'
    client.get_issues_by_status.assert_called_once_with(IssueStatus.ACTIVE)
    mock_subtensor.assert_called_once()


def test_vote_explain_missing_github_token_exits_non_zero(cli_root, runner, tmp_path):
    pat_file = tmp_path / 'miner_pats.json'
    pat_file.write_text('[]')

    result = runner.invoke(
        cli_root,
        ['vote', 'explain', '--json', '--pat-file', str(pat_file)],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert payload['error']['type'] == 'missing_github_token'
