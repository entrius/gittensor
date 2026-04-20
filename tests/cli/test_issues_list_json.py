# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for `issues list --json --id` not-found handling."""

import json
from unittest.mock import patch

FAKE_ISSUES = [
    {
        'id': 1,
        'repository_full_name': 'owner/repo',
        'issue_number': 10,
        'bounty_amount': 50_000_000_000,
        'target_bounty': 100_000_000_000,
        'status': 'Active',
    },
]


def test_issues_list_json_missing_issue_returns_structured_error(cli_root, runner):
    """Requesting a nonexistent issue ID must return a structured JSON error with non-zero exit."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json', '--id', '999'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.output)
    assert payload['success'] is False
    assert payload['error']['type'] == 'not_found'
    assert '999' in payload['error']['message']
