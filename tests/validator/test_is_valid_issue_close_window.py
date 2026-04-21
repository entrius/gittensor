# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for the directional is_valid_issue close-window check.

An issue must close at or shortly AFTER pr.merged_at (within a small clock-skew
buffer, up to MAX_ISSUE_CLOSE_WINDOW_DAYS). Issues closed before the PR merged
are rejected — they were closed by something other than this PR, so the PR
should not inherit their multiplier.
"""

from datetime import datetime, timedelta, timezone

from gittensor.constants import (
    ISSUE_CLOSE_CLOCK_SKEW_SECONDS,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
)
from gittensor.validator.oss_contributions.scoring import is_valid_issue


def _make_pr_and_issue(pr_factory, issue_factory, *, merged_at, closed_at):
    pr = pr_factory.merged(merged_at=merged_at)
    pr.author_login = 'miner_user'
    pr.created_at = merged_at - timedelta(days=2)
    pr.last_edited_at = None

    issue = issue_factory.create(
        author_login='other_user',
        created_at=merged_at - timedelta(days=5),
        closed_at=closed_at,
    )
    return pr, issue


def test_issue_closed_at_merge_is_valid(pr_factory, issue_factory):
    """Common case: PR merge triggers auto-close; timestamps coincide."""
    now = datetime.now(timezone.utc)
    pr, issue = _make_pr_and_issue(pr_factory, issue_factory, merged_at=now, closed_at=now)
    assert is_valid_issue(issue, pr) is True


def test_issue_closed_shortly_after_merge_is_valid(pr_factory, issue_factory):
    """GitHub auto-close can lag merge by a few seconds."""
    now = datetime.now(timezone.utc)
    pr, issue = _make_pr_and_issue(
        pr_factory, issue_factory, merged_at=now, closed_at=now + timedelta(seconds=5)
    )
    assert is_valid_issue(issue, pr) is True


def test_issue_closed_well_after_merge_is_rejected(pr_factory, issue_factory):
    """Pre-existing upper bound: close beyond 1 day after merge looks unrelated."""
    now = datetime.now(timezone.utc)
    pr, issue = _make_pr_and_issue(
        pr_factory,
        issue_factory,
        merged_at=now,
        closed_at=now + timedelta(days=MAX_ISSUE_CLOSE_WINDOW_DAYS, seconds=1),
    )
    assert is_valid_issue(issue, pr) is False


def test_issue_closed_within_skew_buffer_is_valid(pr_factory, issue_factory):
    """Clock skew: closed_at may read slightly earlier than merged_at."""
    now = datetime.now(timezone.utc)
    pr, issue = _make_pr_and_issue(
        pr_factory,
        issue_factory,
        merged_at=now,
        closed_at=now - timedelta(seconds=ISSUE_CLOSE_CLOCK_SKEW_SECONDS // 2),
    )
    assert is_valid_issue(issue, pr) is True


def test_issue_closed_beyond_skew_buffer_is_rejected(pr_factory, issue_factory):
    """Regression: unrelated PR linking an issue already closed by prior work."""
    now = datetime.now(timezone.utc)
    pr, issue = _make_pr_and_issue(
        pr_factory,
        issue_factory,
        merged_at=now,
        closed_at=now - timedelta(seconds=ISSUE_CLOSE_CLOCK_SKEW_SECONDS + 1),
    )
    assert is_valid_issue(issue, pr) is False


def test_issue_closed_23h_before_merge_is_rejected(pr_factory, issue_factory):
    """Exploit documented in the bug report: legit issue closed ~23h earlier."""
    now = datetime.now(timezone.utc)
    pr, issue = _make_pr_and_issue(
        pr_factory, issue_factory, merged_at=now, closed_at=now - timedelta(hours=23)
    )
    assert is_valid_issue(issue, pr) is False
