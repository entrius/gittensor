# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for the issue CLI ParamType subclasses."""

import click
import pytest

from gittensor.cli.issue_commands.helpers import MAX_ISSUE_ID, MAX_ISSUE_NUMBER
from gittensor.cli.issue_commands.types import CONTRACT_ISSUE, GITHUB_ISSUE, REPO, SS58

# A representative valid SS58 address (Alice's well-known dev address).
VALID_SS58 = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'


@pytest.fixture
def ss58_param():
    """Click passes a bound Parameter at runtime; tests use a real Argument so type-checks pass."""
    return click.Argument(['hotkey'])


# =============================================================================
# RepoNameType
# =============================================================================


class TestRepoNameType:
    def test_returns_trimmed_value(self):
        assert REPO.convert('  owner/repo  ', None, None) == 'owner/repo'

    def test_accepts_hyphens_dots_underscores(self):
        assert REPO.convert('my-org/repo.name_v2', None, None) == 'my-org/repo.name_v2'

    @pytest.mark.parametrize('bad', ['no-slash', '/repo', 'owner/', 'owner//repo', 'owner/repo with space'])
    def test_rejects_invalid_format(self, bad):
        with pytest.raises(click.BadParameter):
            REPO.convert(bad, None, None)

    def test_error_message_quotes_input(self):
        with pytest.raises(click.BadParameter) as exc_info:
            REPO.convert('bogus', None, None)
        assert "'bogus'" in str(exc_info.value)


# =============================================================================
# ContractIssueType (on-chain issue ID)
# =============================================================================


class TestContractIssueType:
    def test_accepts_valid(self):
        assert CONTRACT_ISSUE.convert('1', None, None) == 1
        assert CONTRACT_ISSUE.convert(str(MAX_ISSUE_ID - 1), None, None) == MAX_ISSUE_ID - 1

    @pytest.mark.parametrize('bad', ['0', '-1', str(MAX_ISSUE_ID)])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(click.BadParameter):
            CONTRACT_ISSUE.convert(bad, None, None)


# =============================================================================
# GitHubIssueType (GitHub issue number, u32 range)
# =============================================================================


class TestGitHubIssueType:
    def test_accepts_valid(self):
        assert GITHUB_ISSUE.convert('1', None, None) == 1
        assert GITHUB_ISSUE.convert(str(MAX_ISSUE_NUMBER), None, None) == MAX_ISSUE_NUMBER

    def test_accepts_above_contract_cap(self):
        # GitHub issue numbers can exceed the on-chain MAX_ISSUE_ID; that's the whole reason
        # this type exists separately from CONTRACT_ISSUE.
        assert GITHUB_ISSUE.convert(str(MAX_ISSUE_ID), None, None) == MAX_ISSUE_ID

    @pytest.mark.parametrize('bad', ['0', '-1', str(MAX_ISSUE_NUMBER + 1)])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(click.BadParameter):
            GITHUB_ISSUE.convert(bad, None, None)


# =============================================================================
# SS58AddressType
# =============================================================================


class TestSS58AddressType:
    def test_returns_valid_address(self, ss58_param):
        assert SS58.convert(VALID_SS58, ss58_param, None) == VALID_SS58

    def test_rejects_invalid_input(self, ss58_param):
        with pytest.raises(click.BadParameter):
            SS58.convert('not-an-address', ss58_param, None)

    def test_uses_param_name_in_error(self):
        with pytest.raises(click.BadParameter) as exc_info:
            SS58.convert('bad', click.Argument(['solver_hotkey']), None)
        assert 'solver_hotkey' in str(exc_info.value)
