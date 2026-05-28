# The MIT License (MIT)
# Copyright © 2025 Entrius

"""CLI tests for single-issue contract read (no full scan)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import pytest

from gittensor.cli.issue_commands import helpers


def _decoded(issue_id, status_byte=1):
    return SimpleNamespace(
        id=issue_id,
        repository_full_name='entrius/gittensor',
        issue_number=223,
        bounty_amount=1000,
        target_bounty=2000,
        status_byte=status_byte,
    )


def test_read_one_issue_does_a_single_rpc():
    substrate = MagicMock()
    substrate.rpc_request.return_value = {'result': '0x00'}
    with patch.object(helpers, 'decode_issue_from_storage', return_value=_decoded(42, status_byte=1)):
        issue = helpers._read_one_issue_from_child_storage(substrate, '0xchild', 42)

    substrate.rpc_request.assert_called_once()  # one read, not a full scan
    assert issue == {
        'id': 42,
        'repository_full_name': 'entrius/gittensor',
        'issue_number': 223,
        'bounty_amount': 1000,
        'target_bounty': 2000,
        'status': 'Active',
    }


def test_read_one_issue_returns_none_when_absent():
    substrate = MagicMock()
    substrate.rpc_request.return_value = {'result': None}
    assert helpers._read_one_issue_from_child_storage(substrate, '0xchild', 7) is None


def test_fetch_issue_uses_single_read_not_full_scan():
    sample = {
        'id': 42,
        'repository_full_name': 'entrius/gittensor',
        'issue_number': 223,
        'bounty_amount': 1,
        'target_bounty': 2,
        'status': 'Active',
    }
    with (
        patch.object(helpers, 'read_issue_from_contract', return_value=sample) as single,
        patch.object(helpers, 'read_issues_from_contract') as full_scan,
    ):
        result = helpers.fetch_issue_from_contract('ws://x', '0xabc', 42)

    assert result == sample
    single.assert_called_once_with('ws://x', '0xabc', 42, False)
    full_scan.assert_not_called()  # no O(N) scan to find one issue


def test_fetch_issue_not_found_raises():
    with patch.object(helpers, 'read_issue_from_contract', return_value=None):
        with pytest.raises(click.ClickException):
            helpers.fetch_issue_from_contract('ws://x', '0xabc', 99)


def test_fetch_issue_non_bountied_status_raises():
    sample = {
        'id': 42,
        'repository_full_name': 'entrius/gittensor',
        'issue_number': 223,
        'bounty_amount': 1,
        'target_bounty': 2,
        'status': 'Completed',
    }
    with patch.object(helpers, 'read_issue_from_contract', return_value=sample):
        with pytest.raises(click.ClickException):
            helpers.fetch_issue_from_contract('ws://x', '0xabc', 42)
