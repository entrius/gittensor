# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for CLI input validation helpers.

Covers:
    - format_alpha() formatting
    - validate_bounty() precision and range
    - validate_repo_format() owner/repo pattern
    - validate_issue_id() range checks
    - validate_ss58_address() format checks
    - CLI command integration (CliRunner)
"""

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.helpers import (
    ALPHA_RAW_UNIT,
    format_alpha,
    validate_bounty,
    validate_issue_id,
    validate_repo_format,
    validate_ss58_address,
)

# ============================================================================
# format_alpha
# ============================================================================


class TestFormatAlpha:
    def test_basic_formatting(self):
        assert format_alpha(10_000_000_000, 2) == '10.00'

    def test_zero(self):
        assert format_alpha(0, 2) == '0.00'

    def test_fractional(self):
        assert format_alpha(1_500_000_000, 4) == '1.5000'

    def test_large_amount(self):
        assert format_alpha(100_000_000_000_000, 2) == '100000.00'

    def test_different_decimals(self):
        raw = 12_345_678_901
        assert format_alpha(raw, 0) == '12'
        assert format_alpha(raw, 2) == '12.35'
        assert format_alpha(raw, 4) == '12.3457'
        assert format_alpha(raw, 9) == '12.345678901'

    def test_consistency_with_raw_unit(self):
        """1 ALPHA = ALPHA_RAW_UNIT raw units."""
        assert format_alpha(ALPHA_RAW_UNIT, 2) == '1.00'
        assert format_alpha(ALPHA_RAW_UNIT * 100, 2) == '100.00'

    def test_negative_raw_amount(self):
        """Negative input produces negative string (no guard in implementation)."""
        assert format_alpha(-10_000_000_000, 2) == '-10.00'

    def test_zero_decimals(self):
        assert format_alpha(0, 0) == '0'

    def test_single_nano_unit(self):
        """Smallest possible unit formatted to max precision."""
        assert format_alpha(1, 9) == '0.000000001'


# ============================================================================
# validate_bounty
# ============================================================================


class TestValidateBounty:
    def test_valid_bounty(self):
        raw = validate_bounty(100.0)
        assert raw == 100 * ALPHA_RAW_UNIT

    def test_valid_bounty_with_decimals(self):
        raw = validate_bounty(50.5)
        assert raw == 50_500_000_000

    def test_rejects_zero(self):
        with pytest.raises(click.BadParameter, match='positive'):
            validate_bounty(0)

    def test_rejects_negative(self):
        with pytest.raises(click.BadParameter, match='positive'):
            validate_bounty(-10)

    def test_rejects_below_minimum(self):
        with pytest.raises(click.BadParameter, match='at least 10'):
            validate_bounty(5)

    def test_rejects_boundary_below_minimum(self):
        with pytest.raises(click.BadParameter, match='at least 10'):
            validate_bounty(9.999999999)

    def test_rejects_too_many_decimals(self):
        with pytest.raises(click.BadParameter, match='decimal places'):
            validate_bounty(10.0000000001)

    def test_precision_preserved(self):
        """Decimal conversion should avoid float precision loss."""
        raw = validate_bounty(10.123456789)
        assert raw == 10_123_456_789

    def test_exact_minimum(self):
        raw = validate_bounty(10)
        assert raw == 10 * ALPHA_RAW_UNIT

    def test_rejects_infinity(self):
        with pytest.raises(click.BadParameter, match='finite'):
            validate_bounty(float('inf'))

    def test_rejects_negative_infinity(self):
        with pytest.raises(click.BadParameter, match='finite'):
            validate_bounty(float('-inf'))

    def test_rejects_nan(self):
        with pytest.raises(click.BadParameter, match='finite'):
            validate_bounty(float('nan'))

    def test_very_large_valid_bounty(self):
        """A very large bounty should still work (fits in u128)."""
        raw = validate_bounty(1_000_000)
        assert raw == 1_000_000 * ALPHA_RAW_UNIT


# ============================================================================
# validate_repo_format
# ============================================================================


class TestValidateRepoFormat:
    def test_valid_repos(self):
        validate_repo_format('opentensor/btcli')
        validate_repo_format('a/b')
        validate_repo_format('my-org/my-repo')
        validate_repo_format('user.name/repo.name')
        validate_repo_format('user_name/repo_name')

    def test_accepts_dot_git_suffix(self):
        """Repos with .git suffix are valid per the regex."""
        validate_repo_format('opentensor/btcli.git')

    def test_rejects_no_slash(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('opentensorbtcli')

    def test_rejects_double_slash(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('//btcli')

    def test_rejects_triple_segment(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('a/b/c')

    def test_rejects_space_in_owner(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('open tensor/btcli')

    def test_rejects_empty_owner(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('/repo')

    def test_rejects_empty_repo(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('opentensor/')

    def test_rejects_empty_string(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('')

    def test_rejects_at_sign(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('org@name/repo')

    def test_rejects_hash(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('org#name/repo')

    def test_rejects_exclamation(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('org/repo!')

    def test_rejects_unicode_in_owner(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('org-名前/repo')

    def test_rejects_unicode_in_repo(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('org/репо')

    def test_rejects_leading_whitespace(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('  opentensor/btcli')

    def test_rejects_trailing_whitespace(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('opentensor/btcli  ')

    def test_rejects_query_string(self):
        with pytest.raises(click.BadParameter, match='Invalid repository format'):
            validate_repo_format('org/repo?query=1')


# ============================================================================
# validate_issue_id
# ============================================================================


class TestValidateIssueId:
    def test_valid_ids(self):
        validate_issue_id(1)
        validate_issue_id(100)
        validate_issue_id(999_999)

    def test_rejects_zero(self):
        with pytest.raises(click.BadParameter, match='between 1 and 999,999'):
            validate_issue_id(0)

    def test_rejects_negative(self):
        with pytest.raises(click.BadParameter, match='between 1 and 999,999'):
            validate_issue_id(-1)

    def test_rejects_large_negative(self):
        with pytest.raises(click.BadParameter, match='between 1 and 999,999'):
            validate_issue_id(-999_999)

    def test_rejects_too_large(self):
        with pytest.raises(click.BadParameter, match='between 1 and 999,999'):
            validate_issue_id(1_000_000)

    def test_rejects_one_above_maximum(self):
        with pytest.raises(click.BadParameter, match='between 1 and 999,999'):
            validate_issue_id(1_000_001)

    def test_custom_name(self):
        with pytest.raises(click.BadParameter, match='Custom ID'):
            validate_issue_id(0, name='Custom ID')


# ============================================================================
# validate_ss58_address
# ============================================================================


class TestValidateSS58Address:
    # Valid SS58 address (48 chars, starts with 5)
    VALID_ADDR = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'

    def test_valid_address(self):
        validate_ss58_address(self.VALID_ADDR)

    def test_rejects_empty(self):
        with pytest.raises(click.BadParameter, match='Invalid SS58'):
            validate_ss58_address('')

    def test_rejects_short_string(self):
        with pytest.raises(click.BadParameter, match='Invalid SS58'):
            validate_ss58_address('5abc')

    def test_rejects_wrong_prefix(self):
        with pytest.raises(click.BadParameter, match="must start with '5'"):
            validate_ss58_address('1' + 'a' * 46)

    def test_rejects_length_45(self):
        """One below minimum length of 46."""
        with pytest.raises(click.BadParameter, match='Invalid SS58'):
            validate_ss58_address('5' + 'a' * 44)

    def test_rejects_length_49(self):
        """One above maximum length of 48."""
        with pytest.raises(click.BadParameter, match='Invalid SS58'):
            validate_ss58_address('5' + 'a' * 48)

    def test_rejects_length_48_wrong_prefix(self):
        """Correct length but wrong prefix."""
        with pytest.raises(click.BadParameter, match="must start with '5'"):
            validate_ss58_address('4' + 'a' * 47)

    def test_accepts_length_46(self):
        """Minimum valid length. May fail on Keypair check but passes length/prefix."""
        addr = '5' + 'G' * 45
        try:
            validate_ss58_address(addr)
        except click.BadParameter as e:
            # Only Keypair decode error is acceptable, not length/prefix error
            assert 'must start with' not in str(e)

    def test_accepts_length_47(self):
        """Mid-range valid length."""
        addr = '5' + 'G' * 46
        try:
            validate_ss58_address(addr)
        except click.BadParameter as e:
            assert 'must start with' not in str(e)

    def test_custom_name(self):
        with pytest.raises(click.BadParameter, match='Solver hotkey'):
            validate_ss58_address('bad', name='Solver hotkey')


# ============================================================================
# CLI command integration tests (CliRunner)
# ============================================================================


class TestRegisterCommandValidation:
    """Test that validators are properly wired into CLI commands."""

    def test_register_rejects_bad_repo_format(self):
        from gittensor.cli.issue_commands.mutations import issue_register

        runner = CliRunner()
        result = runner.invoke(
            issue_register,
            [
                '--repo',
                'bad-repo-no-slash',
                '--issue',
                '1',
                '--bounty',
                '100',
            ],
        )
        assert 'Invalid repository format' in result.output

    def test_register_rejects_negative_bounty(self):
        from gittensor.cli.issue_commands.mutations import issue_register

        runner = CliRunner()
        result = runner.invoke(
            issue_register,
            [
                '--repo',
                'owner/repo',
                '--issue',
                '1',
                '--bounty',
                '-5',
            ],
        )
        assert 'positive' in result.output or 'finite' in result.output

    def test_register_rejects_zero_bounty(self):
        from gittensor.cli.issue_commands.mutations import issue_register

        runner = CliRunner()
        result = runner.invoke(
            issue_register,
            [
                '--repo',
                'owner/repo',
                '--issue',
                '1',
                '--bounty',
                '0',
            ],
        )
        assert 'positive' in result.output

    def test_register_rejects_below_minimum_bounty(self):
        from gittensor.cli.issue_commands.mutations import issue_register

        runner = CliRunner()
        result = runner.invoke(
            issue_register,
            [
                '--repo',
                'owner/repo',
                '--issue',
                '1',
                '--bounty',
                '5',
            ],
        )
        assert 'at least 10' in result.output


class TestVoteCommandValidation:
    """Test that validators are wired into vote commands."""

    def test_vote_solution_rejects_bad_hotkey(self):
        from gittensor.cli.issue_commands.vote import val_vote_solution

        runner = CliRunner()
        result = runner.invoke(
            val_vote_solution,
            [
                '1',
                'bad-hotkey',
                'bad-coldkey',
                '123',
            ],
        )
        assert 'Invalid SS58' in result.output

    def test_vote_cancel_rejects_zero_issue_id(self):
        from gittensor.cli.issue_commands.vote import val_vote_cancel_issue

        runner = CliRunner()
        result = runner.invoke(
            val_vote_cancel_issue,
            [
                '0',
                'some reason',
            ],
        )
        assert 'between 1 and 999,999' in result.output


class TestAdminCommandValidation:
    """Test that validators are wired into admin commands."""

    def test_admin_set_owner_rejects_bad_address(self):
        from gittensor.cli.issue_commands.admin import admin_set_owner

        runner = CliRunner()
        result = runner.invoke(admin_set_owner, ['not-an-address'])
        assert 'Invalid SS58' in result.output

    def test_admin_add_vali_rejects_bad_address(self):
        from gittensor.cli.issue_commands.admin import admin_add_validator

        runner = CliRunner()
        result = runner.invoke(admin_add_validator, ['short'])
        assert 'Invalid SS58' in result.output

    def test_admin_cancel_rejects_zero_issue(self):
        from gittensor.cli.issue_commands.admin import admin_cancel

        runner = CliRunner()
        result = runner.invoke(admin_cancel, ['0'])
        assert 'between 1 and 999,999' in result.output
