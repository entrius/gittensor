# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for issue discovery scoring: transfer-detection anti-gaming path."""

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


def test_transferred_issue_in_pr_path_counts_as_closed_not_solved(
    issue_factory, pr_factory,
):
    issue = issue_factory.transferred()
    pr = pr_factory.merged()
    pr.issues = [issue]

    miner_evaluations = {1: _make_evaluation(pr)}
    github_id_to_uid = {issue.author_github_id: 1}
    discoverer_data = {issue.author_github_id: _DiscovererData()}

    _collect_issues_from_prs(
        miner_evaluations, github_id_to_uid, discoverer_data, {},
    )

    data = discoverer_data[issue.author_github_id]
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_non_transferred_issue_in_pr_path_counts_as_solved(
    issue_factory, pr_factory,
):
    issue = issue_factory.create()
    pr = pr_factory.merged()
    pr.issues = [issue]

    miner_evaluations = {1: _make_evaluation(pr)}
    github_id_to_uid = {issue.author_github_id: 1}
    discoverer_data = {issue.author_github_id: _DiscovererData()}

    _collect_issues_from_prs(
        miner_evaluations, github_id_to_uid, discoverer_data, {},
    )

    data = discoverer_data[issue.author_github_id]
    assert data.solved_count == 1
    assert data.closed_count == 0


def test_transferred_scan_issue_counts_as_closed(issue_factory):
    issue = issue_factory.transferred()
    scan_issues = {issue.author_github_id: [issue]}
    github_id_to_uid = {issue.author_github_id: 1}
    discoverer_data = {issue.author_github_id: _DiscovererData()}

    _merge_scan_issues(scan_issues, github_id_to_uid, discoverer_data)

    data = discoverer_data[issue.author_github_id]
    assert data.closed_count == 1
    assert data.solved_count == 0


def test_non_transferred_scan_issue_with_closed_at_counts_as_solved(issue_factory):
    issue = issue_factory.create()
    scan_issues = {issue.author_github_id: [issue]}
    github_id_to_uid = {issue.author_github_id: 1}
    discoverer_data = {issue.author_github_id: _DiscovererData()}

    _merge_scan_issues(scan_issues, github_id_to_uid, discoverer_data)

    data = discoverer_data[issue.author_github_id]
    assert data.solved_count == 1
    assert data.closed_count == 0
