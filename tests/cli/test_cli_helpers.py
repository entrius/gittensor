# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit and integration tests for issue CLI helpers and command wiring.

Covers: format_alpha, validate_bounty_amount, validate_repository,
validate_issue_id, validate_ss58_address, colorize_status, and CLI
invocation with validation (no live network).
"""

import json
from decimal import Decimal
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.helpers import (
    ALPHA_DECIMALS,
    ALPHA_RAW_UNIT,
    MAX_BOUNTY_ALPHA,
    MAX_ISSUE_ID,
    MAX_ISSUE_NUMBER,
    STATUS_COLORS,
    colorize_status,
    format_alpha,
    validate_bounty_amount,
    validate_github_issue,
    validate_issue_id,
    validate_repository,
    validate_ss58_address,
)
from gittensor.cli.issue_commands.vote import parse_pr_number

# =============================================================================
# format_alpha
# =============================================================================


class TestFormatAlpha:
    def test_zero(self):
        assert format_alpha(0) == '0.00'
        assert format_alpha(0, decimals=4) == '0.0000'

    def test_small_fractional(self):
        # 0.000000001 raw = 1 nanoALPHA
        assert format_alpha(1, decimals=9) == '0.000000001'

    def test_whole_number(self):
        raw = 100 * ALPHA_RAW_UNIT
        assert format_alpha(raw) == '100.00'
        assert format_alpha(raw, decimals=0) in ('100', '100.')

    def test_large_value(self):
        raw = 1_000_000 * ALPHA_RAW_UNIT
        assert format_alpha(raw) == '1000000.00'

    def test_decimal_precision_no_float_rounding(self):
        # Value that can lose precision with float division
        raw = 1234567890123456789
        out = format_alpha(raw, decimals=9)
        assert out.startswith('1234567890.123')
        assert '123456789' in out

    def test_different_decimals(self):
        raw = 42 * ALPHA_RAW_UNIT
        assert format_alpha(raw, 0) in ('42', '42.')
        assert format_alpha(raw, 2) == '42.00'
        assert format_alpha(raw, 4) == '42.0000'


# =============================================================================
# validate_bounty_amount
# =============================================================================


class TestValidateBountyAmount:
    def test_minimum(self):
        with pytest.raises(Exception) as exc_info:
            validate_bounty_amount('9')
        assert 'Minimum' in str(exc_info.value) or '9' in str(exc_info.value)

    def test_at_minimum_ok(self):
        raw = validate_bounty_amount('10')
        assert raw == 10 * ALPHA_RAW_UNIT

    def test_max_decimals_rejected(self):
        with pytest.raises(Exception):
            validate_bounty_amount('10.1234567891')

    def test_max_decimals_ok(self):
        raw = validate_bounty_amount('10.123456789')
        assert raw == int(Decimal('10.123456789') * ALPHA_RAW_UNIT)

    def test_negative_rejected(self):
        with pytest.raises(Exception):
            validate_bounty_amount('-1')

    def test_inf_rejected(self):
        with pytest.raises(Exception):
            validate_bounty_amount('inf')

    def test_nan_rejected(self):
        with pytest.raises(Exception):
            validate_bounty_amount('nan')

    def test_precision_preserved_via_string(self):
        raw = validate_bounty_amount('10.1')
        assert raw == 10_100_000_000

    def test_large_bounty_ok(self):
        raw = validate_bounty_amount('1000000.5')
        assert raw == 1_000_000_500_000_000

    def test_zero_bounty_rejected(self):
        with pytest.raises(click.BadParameter):
            validate_bounty_amount('0')

    def test_bounty_bad_parameter_has_param_hint(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_bounty_amount('5')
        assert exc_info.value.param_hint == '--bounty'

    def test_invalid_string_rejected(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_bounty_amount('abc')
        assert 'Invalid number' in str(exc_info.value)

    def test_empty_string_rejected(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_bounty_amount('   ')
        assert 'empty' in str(exc_info.value).lower()

    def test_bounty_at_max_accepted(self):
        raw = validate_bounty_amount('100000000')
        assert raw == 100_000_000 * ALPHA_RAW_UNIT

    def test_bounty_over_max_rejected(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_bounty_amount('100000001')
        assert '100,000,000' in str(exc_info.value) or 'exceed' in str(exc_info.value).lower()
        assert exc_info.value.param_hint == '--bounty'


# =============================================================================
# parse_pr_number (vote)
# =============================================================================


class TestParsePrNumber:
    def test_plain_number(self):
        assert parse_pr_number('123') == 123
        assert parse_pr_number('1') == 1

    def test_url_with_pull(self):
        assert parse_pr_number('https://github.com/owner/repo/pull/456') == 456
        assert parse_pr_number('https://github.com/a/b/pull/99') == 99

    def test_invalid_raises(self):
        with pytest.raises(ValueError) as exc_info:
            parse_pr_number('not-a-number')
        assert 'Cannot parse' in str(exc_info.value) or 'not-a-number' in str(exc_info.value)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_pr_number('')


# =============================================================================
# validate_repository (format only when verify_exists=False)
# =============================================================================


class TestValidateRepository:
    def test_valid_format(self):
        owner, name = validate_repository('owner/repo', verify_exists=False)
        assert owner == 'owner'
        assert name == 'repo'

    def test_valid_with_hyphens_dots(self):
        owner, name = validate_repository('my-org/repo.name', verify_exists=False)
        assert owner == 'my-org'
        assert name == 'repo.name'

    def test_missing_slash_rejected(self):
        with pytest.raises(Exception):
            validate_repository('ownerrepo', verify_exists=False)

    def test_empty_part_rejected(self):
        with pytest.raises(Exception):
            validate_repository('/repo', verify_exists=False)
        with pytest.raises(Exception):
            validate_repository('owner/', verify_exists=False)

    def test_invalid_chars_rejected(self):
        with pytest.raises(Exception):
            validate_repository('owner/repo with space', verify_exists=False)

    def test_strips_whitespace(self):
        owner, name = validate_repository('  owner/repo  ', verify_exists=False)
        assert owner == 'owner'
        assert name == 'repo'

    def test_double_slash_rejected(self):
        with pytest.raises(click.BadParameter):
            validate_repository('owner//repo', verify_exists=False)

    def test_repo_bad_parameter_has_param_hint(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_repository('no-slash', verify_exists=False)
        assert exc_info.value.param_hint == '--repo'


# =============================================================================
# validate_issue_id
# =============================================================================


class TestValidateIssueId:
    def test_valid_range(self):
        assert validate_issue_id(1) == 1
        assert validate_issue_id(MAX_ISSUE_ID - 1) == MAX_ISSUE_ID - 1

    def test_zero_rejected(self):
        with pytest.raises(Exception):
            validate_issue_id(0)

    def test_negative_rejected(self):
        with pytest.raises(Exception):
            validate_issue_id(-1)

    def test_at_max_rejected(self):
        with pytest.raises(Exception):
            validate_issue_id(MAX_ISSUE_ID)

    def test_custom_param_name_in_message(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_issue_id(0, param_name='issue_id')
        msg = str(exc_info.value)
        assert '999999' in msg or '1' in msg
        assert exc_info.value.param_hint == 'issue_id'


# =============================================================================
# validate_github_issue
# =============================================================================


class TestValidateGitHubIssue:
    def test_closed_issue_warns_and_returns_data(self):
        """Issue #210 Task 3: closed → warn 'Issue #{number} is already closed.', do not reject."""
        issue_data = {'state': 'closed', 'number': 42, 'title': 'Test'}
        mock_resp = type('Resp', (), {'read': lambda self: json.dumps(issue_data).encode()})()
        with patch('urllib.request.urlopen', return_value=mock_resp):
            with patch('gittensor.cli.issue_commands.helpers.console.print') as mock_print:
                result = validate_github_issue('owner', 'repo', 42)
        assert result == issue_data
        mock_print.assert_called_once()
        call_args = mock_print.call_args[0][0]
        assert 'Issue #42 is already closed' in call_args


# =============================================================================
# validate_ss58_address
# =============================================================================


class TestValidateSs58Address:
    def test_empty_rejected(self):
        with pytest.raises(Exception):
            validate_ss58_address('')

    def test_whitespace_stripped(self):
        # Use regex fallback if scalecodec fails; a 47-char base58 string may pass
        addr = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
        result = validate_ss58_address(f'  {addr}  ')
        assert result == addr

    def test_invalid_short_rejected(self):
        with pytest.raises(Exception):
            validate_ss58_address('short')

    def test_invalid_chars_rejected(self):
        with pytest.raises(Exception):
            validate_ss58_address('0' * 47)

    def test_ss58_bad_parameter_has_param_hint(self):
        with pytest.raises(click.BadParameter) as exc_info:
            validate_ss58_address('', param_name='solver_hotkey')
        assert exc_info.value.param_hint == 'solver_hotkey'


# =============================================================================
# Constants
# =============================================================================


class TestConstants:
    def test_alpha_decimals(self):
        assert ALPHA_DECIMALS == 9

    def test_max_issue_id(self):
        assert MAX_ISSUE_ID == 1_000_000

    def test_max_bounty_alpha(self):
        assert MAX_BOUNTY_ALPHA == 100_000_000

    def test_max_issue_number_u32_friendly(self):
        assert MAX_ISSUE_NUMBER == 2**32 - 1

    def test_format_alpha_uses_raw_unit(self):
        assert format_alpha(ALPHA_RAW_UNIT) == '1.00'
        assert format_alpha(ALPHA_RAW_UNIT, decimals=4) == '1.0000'


# =============================================================================
# colorize_status
# =============================================================================


class TestColorizeStatus:
    def test_known_statuses(self):
        assert 'green' in colorize_status('Active')
        assert 'yellow' in colorize_status('Registered')
        assert 'dim' in colorize_status('Completed')
        assert 'dim' in colorize_status('Cancelled')

    def test_unknown_status_white(self):
        out = colorize_status('Unknown')
        assert 'white' in out or out == '[white]Unknown[/white]'

    def test_status_colors_dict(self):
        assert STATUS_COLORS['Active'] == 'green'
        assert STATUS_COLORS['Registered'] == 'yellow'


# =============================================================================
# CLI integration (validators wired; no live network)
# =============================================================================


def _get_cli_root():
    """Return the root Click group that has 'issues' and 'vote' registered."""
    try:
        from gittensor.cli.main import cli

        return cli
    except ImportError:
        import click

        from gittensor.cli.issue_commands import register_commands

        root = click.Group()
        register_commands(root)
        return root


@pytest.fixture
def cli_root():
    return _get_cli_root()


@pytest.fixture
def runner():
    return CliRunner()


class TestCliRegisterValidation:
    """Ensure register command rejects bad input before any network call."""

    def test_register_rejects_low_bounty(self, cli_root, runner):
        with (
            patch(
                'gittensor.cli.issue_commands.mutations.get_contract_address',
                return_value='0x1234567890123456789012345678901234567890',
            ),
            patch('gittensor.cli.issue_commands.mutations.validate_repository', return_value=('owner', 'repo')),
            patch('gittensor.cli.issue_commands.mutations.validate_github_issue', return_value={}),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'register', '--repo', 'owner/repo', '--issue', '1', '--bounty', '5', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Minimum' in result.output or '5' in result.output

    def test_register_rejects_bad_repo_format(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.mutations.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'register', '--repo', 'no-slash', '--issue', '1', '--bounty', '10', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'owner/repo' in result.output or 'Repository' in result.output

    def test_register_rejects_issue_zero(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.mutations.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'register', '--repo', 'a/b', '--issue', '0', '--bounty', '10', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'between' in result.output or '0' in result.output or 'issue' in result.output.lower()

    def test_register_rejects_issue_number_over_max(self, cli_root, runner):
        over_max = str(MAX_ISSUE_NUMBER + 1)
        with (
            patch(
                'gittensor.cli.issue_commands.mutations.get_contract_address',
                return_value='0x1234567890123456789012345678901234567890',
            ),
            patch('gittensor.cli.issue_commands.mutations.validate_repository', return_value=('a', 'b')),
            patch('gittensor.cli.issue_commands.mutations.validate_github_issue', return_value={}),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'register', '--repo', 'a/b', '--issue', over_max, '--bounty', '10', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'between' in result.output or over_max in result.output or 'issue' in result.output.lower()


class TestCliVoteValidation:
    """Ensure vote solution rejects invalid issue_id / PR (validators wired)."""

    def test_vote_solution_rejects_issue_id_zero(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.vote.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                [
                    'vote',
                    'solution',
                    '0',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '1',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'between' in result.output or '1' in result.output or 'issue' in result.output.lower()

    def test_vote_solution_rejects_pr_zero(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.vote.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                [
                    'vote',
                    'solution',
                    '1',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '0',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'PR' in result.output or 'positive' in result.output

    def test_vote_solution_rejects_invalid_pr(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.vote.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                [
                    'vote',
                    'solution',
                    '1',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    'not-a-pr',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Cannot parse' in result.output or 'not-a-pr' in result.output or 'pr_number' in result.output.lower()


class TestCliAdminValidation:
    """Ensure admin cancel and payout reject invalid issue_id (validator wired)."""

    def test_admin_cancel_rejects_issue_id_zero(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.admin.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                ['admin', 'cancel-issue', '0'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'between' in result.output or '1' in result.output

    def test_admin_payout_rejects_issue_id_zero(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.admin.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                ['admin', 'payout-issue', '0'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'between' in result.output or '1' in result.output


class TestCliVoteCancelValidation:
    """Ensure vote cancel rejects invalid issue_id."""

    def test_vote_cancel_rejects_issue_id_zero(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.vote.get_contract_address',
            return_value='0x1234567890123456789012345678901234567890',
        ):
            result = runner.invoke(
                cli_root,
                ['vote', 'cancel', '0', 'reason text'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'between' in result.output or '1' in result.output


class TestCliMissingContractConfig:
    """Ensure missing contract config exits non-zero."""

    def test_register_missing_contract_fails(self, cli_root, runner):
        with patch('gittensor.cli.issue_commands.mutations.get_contract_address', return_value=''):
            result = runner.invoke(
                cli_root,
                ['issues', 'register', '--repo', 'a/b', '--issue', '1', '--bounty', '10', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output

    def test_vote_missing_contract_fails(self, cli_root, runner):
        with patch('gittensor.cli.issue_commands.vote.get_contract_address', return_value=''):
            result = runner.invoke(
                cli_root,
                [
                    'vote',
                    'solution',
                    '1',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    '1',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output

    def test_admin_missing_contract_fails(self, cli_root, runner):
        with patch('gittensor.cli.issue_commands.admin.get_contract_address', return_value=''):
            result = runner.invoke(
                cli_root,
                ['admin', 'cancel-issue', '1'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output

    def test_harvest_missing_contract_fails(self, cli_root, runner):
        with patch('gittensor.cli.issue_commands.mutations.get_contract_address', return_value=''):
            result = runner.invoke(
                cli_root,
                ['harvest'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output
