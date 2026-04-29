# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for is_valid_issue (issue #605).

Verifies that is_valid_issue mirrors the anti-gaming state_reason gate already
applied in issue_discovery/scoring.py: only state_reason == 'COMPLETED' grants
the 1.33x / 1.66x issue multiplier. Anything else (NOT_PLANNED, TRANSFERRED,
DUPLICATE, None) is rejected.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from gittensor.constants import MAX_ISSUE_CLOSE_WINDOW_DAYS
from gittensor.validator.oss_contributions.scoring import is_valid_issue


class TestIsValidIssueStateReasonGate:
    """Lock down the state_reason gate symmetry with issue_discovery/scoring.py."""

    @pytest.mark.parametrize(
        'state_reason,expected',
        [
            ('COMPLETED', True),
            ('NOT_PLANNED', False),
            ('TRANSFERRED', False),
            ('DUPLICATE', False),
            (None, False),
        ],
    )
    def test_only_completed_state_reason_is_valid(self, pr_factory, issue_factory, state_reason, expected):
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=1)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=5),
            closed_at=now,
            state='CLOSED',
            state_reason=state_reason,
        )

        assert is_valid_issue(issue, pr) is expected


class TestIsValidIssueCloseWindow:
    """Close-window must be directional: reject issues closed before pr.merged_at."""

    @pytest.mark.parametrize(
        'close_offset,expected',
        [
            (timedelta(0), True),
            (timedelta(days=MAX_ISSUE_CLOSE_WINDOW_DAYS, seconds=1), False),
            (timedelta(seconds=-1), False),
            (timedelta(hours=-23), False),
        ],
    )
    def test_close_window_is_directional(self, pr_factory, issue_factory, close_offset, expected):
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=2)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=5),
            closed_at=now + close_offset,
        )

        assert is_valid_issue(issue, pr) is expected


class TestIsValidIssueOpenPRCollateral:
    @pytest.mark.parametrize(
        'state_reason,expected',
        [
            ('COMPLETED', True),
            ('NOT_PLANNED', False),
            ('TRANSFERRED', False),
            ('DUPLICATE', False),
            (None, False),
        ],
    )
    def test_open_pr_rejects_closed_issue_when_not_completed(self, pr_factory, issue_factory, state_reason, expected):
        now = datetime.now(timezone.utc)
        pr = pr_factory.open()
        pr.author_login = 'miner_user'
        pr.created_at = now

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=5),
            closed_at=now - timedelta(hours=1),
            state='CLOSED',
            state_reason=state_reason,
        )

        assert is_valid_issue(issue, pr) is expected

    def test_open_pr_accepts_open_issue(self, pr_factory, issue_factory):
        now = datetime.now(timezone.utc)
        pr = pr_factory.open()
        pr.author_login = 'miner_user'
        pr.created_at = now

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=5),
            closed_at=None,
            state='OPEN',
            state_reason=None,
        )

        assert is_valid_issue(issue, pr) is True


class TestIsValidIssueSkipWarningsCarryPRContext:
    """Each skip warning in `is_valid_issue` (and its `_is_completed_when_closed`
    helper) must include the PR number and repository so a single skipped issue
    in a noisy multi-miner round can be traced back without cross-referencing
    the surrounding `calculate_issue_multiplier` log line. Mirror parity with
    `_is_valid_linked_issue` and same observability shape as the recently merged
    PR #743 (`calculate_review_quality_multiplier` PR-context)."""

    def _assert_warning_has_pr_context(self, mock_logging, pr):
        warning_calls = list(mock_logging.warning.call_args_list)
        assert warning_calls, 'expected at least one warning'
        msg = warning_calls[-1].args[0]
        assert f'PR #{pr.number}' in msg, msg
        assert pr.repository_full_name in msg, msg

    @patch('gittensor.validator.oss_contributions.scoring.bt.logging')
    def test_self_authored_warning_includes_pr_context(self, mock_logging, pr_factory, issue_factory):
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=1)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='miner_user',  # same author → self-issue rejection
            created_at=now - timedelta(days=5),
            closed_at=now,
            state='CLOSED',
            state_reason='COMPLETED',
        )

        assert is_valid_issue(issue, pr) is False
        self._assert_warning_has_pr_context(mock_logging, pr)

    @patch('gittensor.validator.oss_contributions.scoring.bt.logging')
    def test_close_window_warning_includes_pr_context(self, mock_logging, pr_factory, issue_factory):
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=2)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=5),
            closed_at=now + timedelta(days=MAX_ISSUE_CLOSE_WINDOW_DAYS, seconds=1),
            state='CLOSED',
            state_reason='COMPLETED',
        )

        assert is_valid_issue(issue, pr) is False
        self._assert_warning_has_pr_context(mock_logging, pr)

    @patch('gittensor.validator.oss_contributions.scoring.bt.logging')
    def test_state_reason_warning_includes_pr_context(self, mock_logging, pr_factory, issue_factory):
        # Routes through `_is_completed_when_closed`; the helper accepts
        # `pr_ctx` so its warning line is consistent with the rest.
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=1)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=5),
            closed_at=now,
            state='CLOSED',
            state_reason='NOT_PLANNED',
        )

        assert is_valid_issue(issue, pr) is False
        self._assert_warning_has_pr_context(mock_logging, pr)

    @patch('gittensor.validator.oss_contributions.scoring.bt.logging')
    def test_missing_author_login_warning_includes_pr_context(self, mock_logging, pr_factory, issue_factory):
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=1)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='',  # missing author triggers the first skip
            created_at=now - timedelta(days=5),
            closed_at=now,
            state='CLOSED',
            state_reason='COMPLETED',
        )

        assert is_valid_issue(issue, pr) is False
        self._assert_warning_has_pr_context(mock_logging, pr)

    @patch('gittensor.validator.oss_contributions.scoring.bt.logging')
    def test_issue_created_after_pr_warning_includes_pr_context(self, mock_logging, pr_factory, issue_factory):
        now = datetime.now(timezone.utc)
        pr = pr_factory.merged(merged_at=now)
        pr.author_login = 'miner_user'
        pr.created_at = now - timedelta(days=5)
        pr.last_edited_at = None

        issue = issue_factory.create(
            author_login='other_user',
            created_at=now - timedelta(days=2),  # AFTER pr.created_at (which is days=5 ago)
            closed_at=now,
            state='CLOSED',
            state_reason='COMPLETED',
        )

        assert is_valid_issue(issue, pr) is False
        self._assert_warning_has_pr_context(mock_logging, pr)
