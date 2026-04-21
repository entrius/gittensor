# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for issue discovery scoring: state_reason anti-gaming gate.

Solved classification requires state_reason == 'COMPLETED'. Any other value
(NOT_PLANNED, TRANSFERRED, None) routes to closed_count.
"""

from datetime import datetime, timezone

from gittensor.classes import MinerEvaluation
from gittensor.validator.issue_discovery.scoring import (
    _collect_issues_from_prs,
    _DiscovererData,
    _merge_scan_issues,
)


def _make_evaluation(pr) -> MinerEvaluation:
    ev = MinerEvaluation(uid=1, hotkey='test_hotkey')
    ev.merged_pull_requests = [pr]
    return ev


def _run_pr_path(issue, pr):
    pr.issues = [issue]
    miner_evaluations = {1: _make_evaluation(pr)}
    github_id_to_uid = {issue.author_github_id: 1}
    discoverer_data = {issue.author_github_id: _DiscovererData()}

    _collect_issues_from_prs(
        miner_evaluations,
        github_id_to_uid,
        discoverer_data,
        {},
    )
    return discoverer_data[issue.author_github_id]


def _run_scan_path(issue):
    scan_issues = {issue.author_github_id: [issue]}
    github_id_to_uid = {issue.author_github_id: 1}
    discoverer_data = {issue.author_github_id: _DiscovererData()}

    _merge_scan_issues(scan_issues, github_id_to_uid, discoverer_data)
    return discoverer_data[issue.author_github_id]


# ---------------------------------------------------------------------------
# PR-linked path (_collect_issues_from_prs)
# ---------------------------------------------------------------------------


def test_completed_issue_in_pr_path_counts_as_solved(issue_factory, pr_factory):
    issue = issue_factory.completed()
    data = _run_pr_path(issue, pr_factory.merged())
    assert data.solved_count == 1
    assert data.closed_count == 0


def test_transferred_issue_in_pr_path_counts_as_closed_not_solved(
    issue_factory,
    pr_factory,
):
    issue = issue_factory.transferred()
    data = _run_pr_path(issue, pr_factory.merged())
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_not_planned_issue_in_pr_path_counts_as_closed_not_solved(
    issue_factory,
    pr_factory,
):
    issue = issue_factory.not_planned()
    data = _run_pr_path(issue, pr_factory.merged())
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_issue_with_no_state_reason_in_pr_path_counts_as_closed(
    issue_factory,
    pr_factory,
):
    """Legacy data path: None state_reason routes to closed_count."""
    issue = issue_factory.no_reason()
    data = _run_pr_path(issue, pr_factory.merged())
    assert data.closed_count == 1
    assert data.solved_count == 0


# ---------------------------------------------------------------------------
# Scan path (_merge_scan_issues)
# ---------------------------------------------------------------------------


def test_completed_scan_issue_with_closed_at_counts_as_solved(issue_factory):
    issue = issue_factory.completed()
    data = _run_scan_path(issue)
    assert data.solved_count == 1
    assert data.closed_count == 0


def test_transferred_scan_issue_counts_as_closed(issue_factory):
    issue = issue_factory.transferred()
    data = _run_scan_path(issue)
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_not_planned_scan_issue_counts_as_closed(issue_factory):
    issue = issue_factory.not_planned()
    data = _run_scan_path(issue)
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_scan_issue_with_no_state_reason_counts_as_closed(issue_factory):
    """Legacy data path: None state_reason routes to closed_count."""
    issue = issue_factory.no_reason()
    data = _run_scan_path(issue)
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_same_issue_in_multiple_prs_credits_once(issue_factory, pr_factory):
    """Same issue referenced by multiple merged PRs counts once per discoverer."""
    issue = issue_factory.completed()
    pr_a, pr_b = pr_factory.merged(), pr_factory.merged()
    pr_a.issues = pr_b.issues = [issue]
    ev = _make_evaluation(pr_a)
    ev.merged_pull_requests = [pr_a, pr_b]
    discoverer_data = {issue.author_github_id: _DiscovererData()}
    _collect_issues_from_prs({1: ev}, {issue.author_github_id: 1}, discoverer_data, {})
    data = discoverer_data[issue.author_github_id]
    assert data.solved_count == 1
    assert data.valid_solved_count == 1


def test_earliest_merged_pr_is_canonical(issue_factory, pr_factory):
    """Earliest-merged PR drives counts regardless of list/iteration order."""
    issue = issue_factory.completed()
    pr_early = pr_factory.merged(merged_at=datetime(2026, 1, 1, tzinfo=timezone.utc), token_score=2.0)
    pr_late = pr_factory.merged(merged_at=datetime(2026, 1, 2, tzinfo=timezone.utc), token_score=10.0)
    pr_early.issues = pr_late.issues = [issue]
    ev = _make_evaluation(pr_early)
    ev.merged_pull_requests = [pr_late, pr_early]  # late listed first; canonical must still be pr_early
    discoverer_data = {issue.author_github_id: _DiscovererData()}
    _collect_issues_from_prs({1: ev}, {issue.author_github_id: 1}, discoverer_data, {})
    data = discoverer_data[issue.author_github_id]
    assert data.solved_count == 1
    assert data.valid_solved_count == 0  # canonical pr_early is below token threshold


def test_transferred_issue_in_multiple_prs_counts_closed_once(issue_factory, pr_factory):
    """Transferred issue referenced by multiple PRs hits closed_count exactly once."""
    issue = issue_factory.transferred()
    pr_a, pr_b = pr_factory.merged(), pr_factory.merged()
    pr_a.issues = pr_b.issues = [issue]
    ev = _make_evaluation(pr_a)
    ev.merged_pull_requests = [pr_a, pr_b]
    discoverer_data = {issue.author_github_id: _DiscovererData()}
    _collect_issues_from_prs({1: ev}, {issue.author_github_id: 1}, discoverer_data, {})
    data = discoverer_data[issue.author_github_id]
    assert data.closed_count == 1
    assert data.solved_count == 0
