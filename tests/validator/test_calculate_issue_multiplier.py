# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for calculate_issue_multiplier maintainer-preference over GraphQL ordering.

When a PR closes multiple valid issues (e.g., one regular-author, one maintainer),
the multiplier must not depend on the order pr.issues was populated in.
"""

from datetime import datetime, timedelta, timezone

from gittensor.constants import (
    MAINTAINER_ISSUE_MULTIPLIER,
    STANDARD_ISSUE_MULTIPLIER,
)
from gittensor.validator.oss_contributions.scoring import calculate_issue_multiplier


def _make_valid_pair(pr_factory, issue_factory):
    """Build a merged PR with two valid issues: one regular, one maintainer."""
    now = datetime.now(timezone.utc)
    pr = pr_factory.merged(merged_at=now)
    pr.author_login = 'miner_user'
    pr.created_at = now - timedelta(days=1)
    pr.last_edited_at = None

    standard_issue = issue_factory.create(
        number=100,
        author_login='regular_user',
        created_at=now - timedelta(days=5),
        closed_at=now + timedelta(hours=1),
    )
    standard_issue.author_association = 'CONTRIBUTOR'

    maintainer_issue = issue_factory.create(
        number=200,
        author_login='maintainer_user',
        created_at=now - timedelta(days=4),
        closed_at=now + timedelta(hours=1),
    )
    maintainer_issue.author_association = 'OWNER'

    return pr, standard_issue, maintainer_issue


def test_maintainer_wins_when_listed_first(pr_factory, issue_factory):
    pr, standard_issue, maintainer_issue = _make_valid_pair(pr_factory, issue_factory)
    pr.issues = [maintainer_issue, standard_issue]
    assert calculate_issue_multiplier(pr) == MAINTAINER_ISSUE_MULTIPLIER


def test_maintainer_wins_when_listed_second(pr_factory, issue_factory):
    """Regression for ordering-dependent multiplier: list[0] was a regular user."""
    pr, standard_issue, maintainer_issue = _make_valid_pair(pr_factory, issue_factory)
    pr.issues = [standard_issue, maintainer_issue]
    assert calculate_issue_multiplier(pr) == MAINTAINER_ISSUE_MULTIPLIER


def test_standard_multiplier_when_no_maintainer_issue(pr_factory, issue_factory):
    now = datetime.now(timezone.utc)
    pr = pr_factory.merged(merged_at=now)
    pr.author_login = 'miner_user'
    pr.created_at = now - timedelta(days=1)
    pr.last_edited_at = None

    issue = issue_factory.create(
        author_login='regular_user',
        created_at=now - timedelta(days=5),
        closed_at=now + timedelta(hours=1),
    )
    issue.author_association = 'CONTRIBUTOR'
    pr.issues = [issue]

    assert calculate_issue_multiplier(pr) == STANDARD_ISSUE_MULTIPLIER


def test_no_valid_issues_returns_one(pr_factory, issue_factory):
    now = datetime.now(timezone.utc)
    pr = pr_factory.merged(merged_at=now)
    pr.author_login = 'miner_user'
    pr.created_at = now - timedelta(days=1)
    pr.last_edited_at = None

    # Self-authored → is_valid_issue rejects.
    self_issue = issue_factory.create(
        author_login='miner_user',
        created_at=now - timedelta(days=5),
        closed_at=now - timedelta(hours=1),
    )
    self_issue.author_association = 'OWNER'
    pr.issues = [self_issue]

    assert calculate_issue_multiplier(pr) == 1.0
