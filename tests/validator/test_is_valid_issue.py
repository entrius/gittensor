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

import pytest

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
