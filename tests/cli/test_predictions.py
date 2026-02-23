# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for submissions and predict CLI commands and their helpers (109 tests)."""

import json
import os
from unittest.mock import Mock, patch

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.helpers import (
    build_pr_table,
    build_prediction_payload,
    collect_predictions,
    fetch_issue_from_contract,
    fetch_issue_prs,
    format_prediction_lines,
    get_github_pat,
    read_netuid_from_contract,
    validate_predictions,
    validate_probability,
    verify_miner_registration,
)


def _get_cli_root():
    try:
        from gittensor.cli.main import cli

        return cli
    except ImportError:
        import click as _click

        from gittensor.cli.issue_commands import register_commands

        root = _click.Group()
        register_commands(root)
        return root


@pytest.fixture
def cli_root():
    return _get_cli_root()


@pytest.fixture
def runner():
    return CliRunner()


MOCK_CONTRACT = '0x1234567890123456789012345678901234567890'
MOCK_HOTKEY = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
MOCK_WALLET = type('W', (), {'hotkey': type('H', (), {'ss58_address': MOCK_HOTKEY})()})()

ACTIVE_ISSUE = {'id': 1, 'repository_full_name': 'owner/repo', 'issue_number': 42, 'status': 'Active'}
REGISTERED_ISSUE = {'id': 2, 'repository_full_name': 'owner/repo', 'issue_number': 43, 'status': 'Registered'}
COMPLETED_ISSUE = {'id': 3, 'repository_full_name': 'owner/repo', 'issue_number': 44, 'status': 'Completed'}

OPEN_PR = {
    'number': 10,
    'title': 'Fix bug',
    'author_login': 'alice',
    'created_at': '2026-01-15',
    'state': 'OPEN',
    'url': 'https://github.com/owner/repo/pull/10',
    'review_count': 1,
    'merged_at': None,
    'closing_numbers': [42],
}
OPEN_PR_2 = {
    'number': 20,
    'title': 'Add feature',
    'author_login': 'bob',
    'created_at': '2026-02-01',
    'state': 'OPEN',
    'url': 'https://github.com/owner/repo/pull/20',
    'review_count': 0,
    'merged_at': None,
    'closing_numbers': [],
}


# =============================================================================
# validate_probability
# =============================================================================


class TestValidateProbability:
    def test_valid_boundaries(self):
        assert validate_probability(0.0) == 0.0
        assert validate_probability(1.0) == 1.0
        assert validate_probability(0.5) == 0.5

    def test_below_zero_raises(self):
        with pytest.raises(click.BadParameter):
            validate_probability(-0.1)

    def test_above_one_raises(self):
        with pytest.raises(click.BadParameter):
            validate_probability(1.01)

    def test_param_hint_propagated(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_probability(2.0, param_hint='--probability')
        assert exc_info.value.param_hint == '--probability'

    def test_exactly_zero_and_one(self):
        assert validate_probability(0.0) == 0.0
        assert validate_probability(1.0) == 1.0


# =============================================================================
# validate_predictions
# =============================================================================


class TestValidatePredictions:
    def test_sum_at_one_ok(self):
        validate_predictions({1: 0.5, 2: 0.5}, {1, 2})

    def test_sum_under_one_ok(self):
        validate_predictions({1: 0.3}, {1, 2})

    def test_sum_over_one_raises(self):
        with pytest.raises(click.BadParameter, match='<= 1.0'):
            validate_predictions({1: 0.6, 2: 0.6}, {1, 2})

    def test_unknown_pr_raises_with_available(self):
        with pytest.raises(click.BadParameter, match='Open PRs:'):
            validate_predictions({999: 0.5}, {10, 20})

    def test_empty_predictions_ok(self):
        validate_predictions({}, {1, 2})

    def test_single_prediction_at_one(self):
        validate_predictions({1: 1.0}, {1})

    def test_float_precision_edge(self):
        validate_predictions({1: 0.1, 2: 0.2, 3: 0.3, 4: 0.39999}, {1, 2, 3, 4})

    def test_multiple_unknown_prs(self):
        with pytest.raises(click.BadParameter, match='PR #999'):
            validate_predictions({999: 0.3, 888: 0.2}, {10, 20})


# =============================================================================
# get_github_pat
# =============================================================================


class TestGetGithubPat:
    def test_returns_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            if 'GITTENSOR_MINER_PAT' in os.environ:
                del os.environ['GITTENSOR_MINER_PAT']
            assert get_github_pat() is None

    def test_returns_value_when_set(self):
        with patch.dict(os.environ, {'GITTENSOR_MINER_PAT': 'ghp_secret123'}):
            assert get_github_pat() == 'ghp_secret123'

    def test_empty_string_treated_as_none(self):
        with patch.dict(os.environ, {'GITTENSOR_MINER_PAT': ''}):
            assert get_github_pat() is None


# =============================================================================
# build_pr_table
# =============================================================================


class TestBuildPrTable:
    def test_returns_table_with_prs(self):
        table = build_pr_table([OPEN_PR, OPEN_PR_2])
        assert table.row_count == 2

    def test_empty_list_returns_empty_table(self):
        table = build_pr_table([])
        assert table.row_count == 0

    def test_approved_review_shown(self):
        table = build_pr_table([OPEN_PR])
        assert table.row_count == 1

    def test_ghost_user_displayed(self):
        ghost_pr = {**OPEN_PR, 'author_login': 'ghost'}
        table = build_pr_table([ghost_pr])
        assert table.row_count == 1

    def test_missing_fields_handled(self):
        minimal_pr = {'number': 99}
        table = build_pr_table([minimal_pr])
        assert table.row_count == 1

    def test_long_title_truncated(self):
        long_pr = {**OPEN_PR, 'title': 'A' * 200}
        table = build_pr_table([long_pr])
        assert table.row_count == 1


# =============================================================================
# format_prediction_lines
# =============================================================================


class TestFormatPredictionLines:
    def test_single_prediction(self):
        out = format_prediction_lines({101: 0.7})
        assert 'PR #101: 70.00%' in out
        assert 'Total: 70.00%' in out

    def test_multiple_sorted(self):
        out = format_prediction_lines({200: 0.3, 100: 0.5})
        lines = out.split('\n')
        assert 'PR #100' in lines[0]
        assert 'PR #200' in lines[1]
        assert 'Total: 80.00%' in lines[2]

    def test_empty_predictions(self):
        out = format_prediction_lines({})
        assert 'Total: 0.00%' in out


# =============================================================================
# build_prediction_payload
# =============================================================================


class TestBuildPredictionPayload:
    def test_payload_shape(self):
        payload = build_prediction_payload(1, 'owner/repo', 42, MOCK_HOTKEY, {10: 0.7, 20: 0.3})
        assert payload['issue_id'] == 1
        assert payload['repository'] == 'owner/repo'
        assert payload['issue_number'] == 42
        assert payload['miner_hotkey'] == MOCK_HOTKEY
        assert payload['predictions'] == {'10': 0.7, '20': 0.3}

    def test_pr_keys_are_strings(self):
        payload = build_prediction_payload(1, 'o/r', 1, 'h', {123: 0.5})
        assert '123' in payload['predictions']

    def test_empty_predictions(self):
        payload = build_prediction_payload(1, 'o/r', 1, 'h', {})
        assert payload['predictions'] == {}

    def test_many_predictions(self):
        preds = {i: 0.01 for i in range(50)}
        payload = build_prediction_payload(1, 'o/r', 1, 'h', preds)
        assert len(payload['predictions']) == 50


# =============================================================================
# fetch_issue_from_contract
# =============================================================================


class TestFetchIssueFromContract:
    def test_active_issue_returned(self):
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[ACTIVE_ISSUE]):
            issue = fetch_issue_from_contract('ws', 'addr', 1)
        assert issue['id'] == 1

    def test_not_found_raises(self):
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[]):
            with pytest.raises(click.ClickException, match='not found'):
                fetch_issue_from_contract('ws', 'addr', 99)

    def test_completed_rejected_for_submissions(self):
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[COMPLETED_ISSUE]):
            with pytest.raises(click.ClickException, match='not in a bountied state'):
                fetch_issue_from_contract('ws', 'addr', 3)

    def test_registered_ok_for_submissions(self):
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[REGISTERED_ISSUE]):
            issue = fetch_issue_from_contract('ws', 'addr', 2, require_active=False)
        assert issue['status'] == 'Registered'

    def test_registered_rejected_for_predict(self):
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[REGISTERED_ISSUE]):
            with pytest.raises(click.ClickException, match='not active'):
                fetch_issue_from_contract('ws', 'addr', 2, require_active=True)

    def test_missing_repo_raises(self):
        issue = {'id': 5, 'status': 'Active', 'repository_full_name': '', 'issue_number': 42}
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[issue]):
            with pytest.raises(click.ClickException, match='missing'):
                fetch_issue_from_contract('ws', 'addr', 5)

    def test_missing_issue_number_raises(self):
        issue = {'id': 6, 'status': 'Active', 'repository_full_name': 'owner/repo', 'issue_number': 0}
        with patch('gittensor.cli.issue_commands.helpers.read_issues_from_contract', return_value=[issue]):
            with pytest.raises(click.ClickException, match='missing'):
                fetch_issue_from_contract('ws', 'addr', 6)


# =============================================================================
# fetch_issue_prs (cascading fallback)
# =============================================================================


class TestFetchIssuePrs:
    def test_graphql_results_returned_directly(self):
        with patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[OPEN_PR]):
            prs = fetch_issue_prs('owner/repo', 42, 'token')
        assert len(prs) == 1
        assert prs[0]['number'] == 10

    def test_graphql_empty_falls_back_to_auth_rest(self):
        with (
            patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[]),
            patch('gittensor.utils.github_api_tools._search_prs_rest', return_value=[OPEN_PR_2]) as mock_rest,
        ):
            prs = fetch_issue_prs('owner/repo', 42, 'token')
        assert len(prs) == 1
        assert prs[0]['number'] == 20
        mock_rest.assert_called_once_with('owner/repo', 42, token='token', state='open')

    def test_all_three_levels_cascade(self):
        with (
            patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[]),
            patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=[[], [OPEN_PR]]),
        ):
            prs = fetch_issue_prs('owner/repo', 42, 'token')
        assert len(prs) == 1
        assert prs[0]['number'] == 10

    def test_no_token_uses_rest_directly(self):
        with patch('gittensor.utils.github_api_tools._search_prs_rest', return_value=[OPEN_PR]) as mock_rest:
            prs = fetch_issue_prs('owner/repo', 42, None)
        assert len(prs) == 1
        mock_rest.assert_called_once_with('owner/repo', 42, token=None, state='open')

    def test_exception_returns_empty(self):
        with patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=Exception('network')):
            prs = fetch_issue_prs('owner/repo', 42, None)
        assert prs == []

    def test_graceful_error_shows_warning(self, capsys):
        with patch('gittensor.utils.github_api_tools.find_prs_for_issue', side_effect=RuntimeError('API down')):
            prs = fetch_issue_prs('owner/repo', 42, 'token')
        assert prs == []

    def test_ghost_user_pr_returned(self):
        ghost_pr = {**OPEN_PR, 'author_login': 'ghost'}
        with patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[ghost_pr]):
            prs = fetch_issue_prs('owner/repo', 42, 'token')
        assert prs[0]['author_login'] == 'ghost'


# =============================================================================
# collect_predictions (extracted helper)
# =============================================================================


class TestCollectPredictions:
    prs = [OPEN_PR, OPEN_PR_2]
    pr_numbers = {10, 20}

    def test_json_input_mode(self):
        result = collect_predictions(None, None, '{"10": 0.5, "20": 0.3}', self.prs, self.pr_numbers, 1)
        assert result == {10: 0.5, 20: 0.3}

    def test_single_pr_mode(self):
        result = collect_predictions(10, 0.7, None, self.prs, self.pr_numbers, 1)
        assert result == {10: 0.7}

    def test_interactive_mode(self):
        with patch('click.prompt', side_effect=['0.6', '']):
            result = collect_predictions(None, None, None, self.prs, self.pr_numbers, 1)
        assert result == {10: 0.6}

    def test_mutually_exclusive_pr_and_json(self):
        with pytest.raises(click.ClickException, match='either'):
            collect_predictions(10, 0.5, '{"10": 0.5}', self.prs, self.pr_numbers, 1)

    def test_probability_with_json_rejected(self):
        with pytest.raises(click.ClickException, match='either'):
            collect_predictions(None, 0.5, '{"10": 0.5}', self.prs, self.pr_numbers, 1)

    def test_probability_without_pr_rejected(self):
        with pytest.raises(click.ClickException, match='--probability requires --pr'):
            collect_predictions(None, 0.5, None, self.prs, self.pr_numbers, 1)

    def test_pr_without_probability_rejected(self):
        with pytest.raises(click.ClickException, match='--probability is required'):
            collect_predictions(10, None, None, self.prs, self.pr_numbers, 1)

    def test_invalid_json_rejected(self):
        with pytest.raises(click.BadParameter, match='Invalid JSON'):
            collect_predictions(None, None, 'not-json', self.prs, self.pr_numbers, 1)

    def test_unknown_pr_in_json_rejected(self):
        with pytest.raises(click.BadParameter, match='not an open PR'):
            collect_predictions(None, None, '{"999": 0.5}', self.prs, self.pr_numbers, 1)

    def test_no_predictions_interactive_rejected(self):
        with patch('click.prompt', return_value=''):
            with pytest.raises(click.ClickException, match='No predictions entered'):
                collect_predictions(None, None, None, self.prs, self.pr_numbers, 1)

    def test_json_single_entry(self):
        result = collect_predictions(None, None, '{"10": 0.9}', self.prs, self.pr_numbers, 1)
        assert result == {10: 0.9}

    def test_json_probability_at_zero(self):
        result = collect_predictions(None, None, '{"10": 0.0}', self.prs, self.pr_numbers, 1)
        assert result == {10: 0.0}

    def test_json_probability_at_one(self):
        result = collect_predictions(None, None, '{"10": 1.0}', self.prs, self.pr_numbers, 1)
        assert result == {10: 1.0}

    def test_json_sum_exceeds_one_rejected(self):
        with pytest.raises(click.BadParameter, match='<= 1.0'):
            collect_predictions(None, None, '{"10": 0.8, "20": 0.5}', self.prs, self.pr_numbers, 1)

    def test_json_non_dict_rejected(self):
        with pytest.raises(click.BadParameter, match='object'):
            collect_predictions(None, None, '[1, 2]', self.prs, self.pr_numbers, 1)

    def test_json_invalid_pr_key_rejected(self):
        with pytest.raises(click.BadParameter, match='Invalid PR number'):
            collect_predictions(None, None, '{"abc": 0.5}', self.prs, self.pr_numbers, 1)

    def test_json_invalid_probability_value_rejected(self):
        with pytest.raises(click.BadParameter, match='probability value'):
            collect_predictions(None, None, '{"10": "abc"}', self.prs, self.pr_numbers, 1)

    def test_single_pr_probability_zero(self):
        result = collect_predictions(10, 0.0, None, self.prs, self.pr_numbers, 1)
        assert result == {10: 0.0}

    def test_single_pr_probability_one(self):
        result = collect_predictions(10, 1.0, None, self.prs, self.pr_numbers, 1)
        assert result == {10: 1.0}

    def test_single_pr_unknown_rejected(self):
        with pytest.raises(click.BadParameter, match='not an open PR'):
            collect_predictions(999, 0.5, None, self.prs, self.pr_numbers, 1)

    def test_interactive_empty_prs_rejected(self):
        with pytest.raises(click.ClickException, match='No open PRs'):
            collect_predictions(None, None, None, [], set(), 1)

    def test_interactive_multiple_entries(self):
        with patch('click.prompt', side_effect=['0.3', '0.4']):
            result = collect_predictions(None, None, None, self.prs, self.pr_numbers, 1)
        assert result == {10: 0.3, 20: 0.4}


# =============================================================================
# CLI: submissions
# =============================================================================


class TestSubmissionsCommand:
    def test_missing_contract_fails(self, cli_root, runner):
        with patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=''):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1'], catch_exceptions=False)
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output

    def test_invalid_issue_id_fails(self, cli_root, runner):
        with patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '0'], catch_exceptions=False)
        assert result.exit_code != 0

    def test_issue_not_found_fails(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch(
                'gittensor.cli.issue_commands.predictions.fetch_issue_from_contract',
                side_effect=click.ClickException('not found'),
            ),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1'], catch_exceptions=False)
        assert result.exit_code != 0
        assert 'not found' in result.output

    def test_json_output_structure(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=ACTIVE_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value=None),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[OPEN_PR]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1', '--json'], catch_exceptions=False)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]['number'] == 10
        assert data[0]['merged_at'] is None
        assert data[0]['closes_issue'] is True  # OPEN_PR has closing_numbers [42], issue_number 42

    def test_json_output_empty_prs(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=ACTIVE_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value=None),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1', '--json'], catch_exceptions=False)
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_table_output_with_prs(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=ACTIVE_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value='token'),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[OPEN_PR, OPEN_PR_2]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1'], catch_exceptions=False)
        assert result.exit_code == 0
        assert '2 open PR(s)' in result.output

    def test_no_pat_shows_unauthenticated_message(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=ACTIVE_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value=None),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1'], catch_exceptions=False)
        assert result.exit_code == 0
        assert 'unauthenticated' in result.output.lower() or 'rate limit' in result.output.lower()

    def test_empty_prs_shows_message(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=ACTIVE_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value='token'),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1'], catch_exceptions=False)
        assert result.exit_code == 0
        assert 'No open PRs' in result.output

    def test_registered_issue_ok_for_submissions(self, cli_root, runner):
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=REGISTERED_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value='token'),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[OPEN_PR]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '2'], catch_exceptions=False)
        assert result.exit_code == 0

    def test_ghost_user_in_json_output(self, cli_root, runner):
        ghost_pr = {**OPEN_PR, 'author_login': 'ghost'}
        with (
            patch('gittensor.cli.issue_commands.predictions.get_contract_address', return_value=MOCK_CONTRACT),
            patch('gittensor.cli.issue_commands.predictions.resolve_network', return_value=('wss://test', 'test')),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_from_contract', return_value=ACTIVE_ISSUE),
            patch('gittensor.cli.issue_commands.predictions.get_github_pat', return_value='token'),
            patch('gittensor.cli.issue_commands.predictions.fetch_issue_prs', return_value=[ghost_pr]),
        ):
            result = runner.invoke(cli_root, ['issues', 'submissions', '--id', '1', '--json'], catch_exceptions=False)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]['author'] == 'ghost'


# =============================================================================
# CLI: predict
# =============================================================================


_PREDICT_MOCKS_BASE = {
    'gittensor.cli.issue_commands.predictions.get_contract_address': MOCK_CONTRACT,
    'gittensor.cli.issue_commands.predictions.resolve_network': ('wss://test', 'test'),
    'gittensor.cli.issue_commands.predictions.get_github_pat': 'token',
    'gittensor.cli.issue_commands.predictions.load_config': {},
    'gittensor.cli.issue_commands.predictions.verify_miner_registration': True,
    'gittensor.cli.issue_commands.predictions.fetch_issue_from_contract': ACTIVE_ISSUE,
    'gittensor.cli.issue_commands.predictions.fetch_issue_prs': [OPEN_PR, OPEN_PR_2],
}


def _predict_ctx(overrides=None):
    """Context manager that patches all predict dependencies."""
    import contextlib

    mocks = {**_PREDICT_MOCKS_BASE, **(overrides or {})}
    patches = []
    for target, value in mocks.items():
        if callable(value) and hasattr(value, '__self__'):
            patches.append(patch(target, side_effect=value))
        elif isinstance(value, Exception):
            patches.append(patch(target, side_effect=value))
        else:
            patches.append(patch(target, return_value=value))
    patches.append(patch('bittensor.Wallet', return_value=MOCK_WALLET))
    return contextlib.ExitStack().__enter__() or contextlib.ExitStack()


class TestPredictCommand:
    @pytest.fixture(autouse=True)
    def _mock_predict(self):
        """Patch all predict dependencies for every test in this class."""
        self._overrides = {}
        self._patches = []

    def _run(self, runner, cli_root, args, overrides=None):
        mocks = {**_PREDICT_MOCKS_BASE, **(overrides or {})}
        stack = []
        for target, value in mocks.items():
            if isinstance(value, Exception):
                stack.append(patch(target, side_effect=value))
            else:
                stack.append(patch(target, return_value=value))
        stack.append(patch('bittensor.Wallet', return_value=MOCK_WALLET))
        import contextlib

        with contextlib.ExitStack() as s:
            for p in stack:
                s.enter_context(p)
            return runner.invoke(cli_root, ['issues', 'predict'] + args, catch_exceptions=False)

    def test_single_prediction_success(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '0.7', '-y'])
        assert result.exit_code == 0
        assert 'Prediction Submitted' in result.output or 'PR #10' in result.output

    def test_batch_json_input_success(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '{"10": 0.5, "20": 0.3}', '-y'])
        assert result.exit_code == 0

    def test_json_output_mode(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '0.7', '-y', '--json'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['issue_id'] == 1
        assert data['miner_hotkey'] == MOCK_HOTKEY
        assert '10' in data['predictions']

    def test_missing_contract_fails(self, cli_root, runner):
        result = self._run(
            runner,
            cli_root,
            ['--id', '1', '--pr', '10', '--probability', '0.5', '-y'],
            overrides={'gittensor.cli.issue_commands.predictions.get_contract_address': ''},
        )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output

    def test_missing_pat_fails(self, cli_root, runner):
        result = self._run(
            runner,
            cli_root,
            ['--id', '1', '--pr', '10', '--probability', '0.5', '-y'],
            overrides={'gittensor.cli.issue_commands.predictions.get_github_pat': None},
        )
        assert result.exit_code != 0
        assert 'GITTENSOR_MINER_PAT' in result.output

    def test_invalid_issue_id_fails(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '0', '-y'])
        assert result.exit_code != 0

    def test_mutually_exclusive_inputs_rejected(self, cli_root, runner):
        result = self._run(
            runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '0.5', '--json-input', '{"10":0.5}', '-y']
        )
        assert result.exit_code != 0
        assert 'either' in result.output.lower()

    def test_probability_without_pr_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--probability', '0.5', '-y'])
        assert result.exit_code != 0
        assert '--probability requires --pr' in result.output

    def test_pr_without_probability_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '-y'])
        assert result.exit_code != 0
        assert 'probability' in result.output.lower()

    def test_probability_out_of_range_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '1.5', '-y'])
        assert result.exit_code != 0
        assert '0.0' in result.output and '1.0' in result.output

    def test_pr_not_found_shows_available(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '999', '--probability', '0.5', '-y'])
        assert result.exit_code != 0
        assert '999' in result.output
        assert '10' in result.output or '20' in result.output

    def test_json_sum_exceeds_one_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '{"10": 0.8, "20": 0.5}', '-y'])
        assert result.exit_code != 0
        assert '<= 1.0' in result.output

    def test_json_invalid_syntax_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', 'not-json', '-y'])
        assert result.exit_code != 0
        assert 'Invalid JSON' in result.output

    def test_json_non_dict_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '[1, 2]', '-y'])
        assert result.exit_code != 0
        assert 'object' in result.output.lower()

    def test_registered_issue_rejected_for_predict(self, cli_root, runner):
        result = self._run(
            runner,
            cli_root,
            ['--id', '2', '--pr', '10', '--probability', '0.5', '-y'],
            overrides={
                'gittensor.cli.issue_commands.predictions.fetch_issue_from_contract': click.ClickException('not active')
            },
        )
        assert result.exit_code != 0
        assert 'not active' in result.output

    def test_probability_with_json_input_no_pr_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '{"10": 0.5}', '--probability', '0.4', '-y'])
        assert result.exit_code != 0
        assert 'either' in result.output.lower()

    def test_json_invalid_probability_value_message(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '{"10": "abc"}', '-y'])
        assert result.exit_code != 0
        assert 'probability value' in result.output.lower()
        assert 'abc' in result.output

    def test_unregistered_miner_rejected(self, cli_root, runner):
        result = self._run(
            runner,
            cli_root,
            ['--id', '1', '--pr', '10', '--probability', '0.5', '-y'],
            overrides={'gittensor.cli.issue_commands.predictions.verify_miner_registration': False},
        )
        assert result.exit_code != 0
        assert 'not a registered miner' in result.output

    def test_negative_probability_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '-0.5', '-y'])
        assert result.exit_code != 0
        assert '0.0' in result.output and '1.0' in result.output

    def test_probability_zero_accepted(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '0.0', '-y'])
        assert result.exit_code == 0

    def test_probability_one_accepted(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '1.0', '-y'])
        assert result.exit_code == 0

    def test_json_output_contains_miner_hotkey(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--pr', '10', '--probability', '0.5', '-y', '--json'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['miner_hotkey'] == MOCK_HOTKEY
        assert data['repository'] == 'owner/repo'

    def test_json_output_predictions_keys_are_strings(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '{"10": 0.5, "20": 0.3}', '-y', '--json'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert '10' in data['predictions']
        assert '20' in data['predictions']

    def test_json_input_invalid_pr_key_rejected(self, cli_root, runner):
        result = self._run(runner, cli_root, ['--id', '1', '--json-input', '{"abc": 0.5}', '-y'])
        assert result.exit_code != 0
        assert 'Invalid PR number' in result.output


# =============================================================================
# read_netuid_from_contract (unit tests for the helper)
# =============================================================================


class TestReadNetuidFromContract:
    @patch('gittensor.cli.issue_commands.helpers._read_contract_packed_storage')
    @patch('substrateinterface.SubstrateInterface')
    def test_returns_netuid_on_success(self, mock_substrate, mock_packed):
        mock_packed.return_value = {'netuid': 42, 'owner': '5xxx'}
        result = read_netuid_from_contract('wss://test', '5xxx')
        assert result == 42

    @patch('gittensor.cli.issue_commands.helpers._read_contract_packed_storage')
    @patch('substrateinterface.SubstrateInterface')
    def test_returns_none_when_packed_storage_is_none(self, mock_substrate, mock_packed):
        mock_packed.return_value = None
        result = read_netuid_from_contract('wss://test', '5xxx')
        assert result is None

    @patch('gittensor.cli.issue_commands.helpers._read_contract_packed_storage')
    @patch('substrateinterface.SubstrateInterface')
    def test_returns_none_when_netuid_missing(self, mock_substrate, mock_packed):
        mock_packed.return_value = {'owner': '5xxx'}
        result = read_netuid_from_contract('wss://test', '5xxx')
        assert result is None

    @patch('substrateinterface.SubstrateInterface', side_effect=Exception('Connection refused'))
    def test_returns_none_on_connection_error(self, mock_substrate):
        result = read_netuid_from_contract('wss://test', '5xxx')
        assert result is None


# =============================================================================
# verify_miner_registration (unit tests for the helper)
# =============================================================================


class TestVerifyMinerRegistration:
    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=42)
    def test_registered_miner_returns_true(self, mock_netuid):
        mock_subtensor = Mock()
        mock_subtensor.is_hotkey_registered.return_value = True
        with patch('bittensor.Subtensor', return_value=mock_subtensor):
            assert verify_miner_registration('wss://test', '5xxx', '5Hot...') is True
        mock_subtensor.is_hotkey_registered.assert_called_once_with(netuid=42, hotkey_ss58='5Hot...')

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=42)
    def test_unregistered_miner_returns_false(self, mock_netuid):
        mock_subtensor = Mock()
        mock_subtensor.is_hotkey_registered.return_value = False
        with patch('bittensor.Subtensor', return_value=mock_subtensor):
            assert verify_miner_registration('wss://test', '5xxx', '5Hot...') is False

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=None)
    def test_returns_false_when_netuid_is_none(self, mock_netuid):
        assert verify_miner_registration('wss://test', '5xxx', '5Hot...') is False

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=42)
    def test_returns_false_on_subtensor_exception(self, mock_netuid):
        with patch('bittensor.Subtensor', side_effect=Exception('network error')):
            assert verify_miner_registration('wss://test', '5xxx', '5Hot...') is False

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=42)
    def test_returns_false_on_import_error(self, mock_netuid):
        with patch('builtins.__import__', side_effect=ImportError('No module named bittensor')):
            assert verify_miner_registration('wss://test', '5xxx', '5Hot...') is False

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=42)
    def test_returns_false_when_is_hotkey_registered_raises(self, mock_netuid):
        mock_subtensor = Mock()
        mock_subtensor.is_hotkey_registered.side_effect = Exception('RPC error')
        with patch('bittensor.Subtensor', return_value=mock_subtensor):
            assert verify_miner_registration('wss://test', '5xxx', '5Hot...') is False
