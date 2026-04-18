# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for issue discovery scoring: state_reason anti-gaming gate.

Solved classification requires state_reason == 'COMPLETED'. Any other value
(NOT_PLANNED, TRANSFERRED, None) routes to closed_count.
"""

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


def test_duplicate_issue_referenced_by_multiple_prs_counts_once(issue_factory, pr_factory):
    issue_a = issue_factory.completed(number=42, repository_full_name='test/repo', author_github_id='1001')
    issue_b = issue_factory.completed(number=42, repository_full_name='test/repo', author_github_id='1001')

    pr_one = pr_factory.merged(number=10, uid=2, repo='test/repo')
    pr_one.issues = [issue_a]
    pr_two = pr_factory.merged(number=20, uid=3, repo='test/repo')
    pr_two.issues = [issue_b]

    miner_evaluations = {
        2: MinerEvaluation(uid=2, hotkey='hotkey_2', github_id='2', merged_pull_requests=[pr_one]),
        3: MinerEvaluation(uid=3, hotkey='hotkey_3', github_id='3', merged_pull_requests=[pr_two]),
    }
    discoverer_data = {'1001': _DiscovererData()}

    _collect_issues_from_prs(miner_evaluations, {'1001': 1}, discoverer_data, {})

    data = discoverer_data['1001']
    assert data.solved_count == 1
    assert data.valid_solved_count == 1
    assert len(data.scored_issues) == 1


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
