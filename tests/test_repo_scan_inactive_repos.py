# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for repo scan handling of inactive repositories."""

import asyncio
import importlib
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace


@dataclass
class FakeIssue:
    number: int
    pr_number: int
    repository_full_name: str
    title: str
    created_at: object = None
    closed_at: object = None
    author_login: str | None = None
    author_github_id: str | None = None
    state: str | None = None
    state_reason: str | None = None


def _install_repo_scan_stubs(monkeypatch):
    fake_bt = SimpleNamespace(
        logging=SimpleNamespace(info=lambda *_args, **_kwargs: None, debug=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None)
    )
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)

    fake_classes = ModuleType('gittensor.classes')
    fake_classes.Issue = FakeIssue
    fake_classes.MinerEvaluation = object
    monkeypatch.setitem(sys.modules, 'gittensor.classes', fake_classes)

    fake_github_tools = ModuleType('gittensor.utils.github_api_tools')
    fake_github_tools.find_solver_from_cross_references = lambda *_args, **_kwargs: (None, None)
    monkeypatch.setitem(sys.modules, 'gittensor.utils.github_api_tools', fake_github_tools)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.issue_discovery.repo_scan', raising=False)


def test_scan_closed_issues_keeps_pre_inactive_closures(monkeypatch):
    _install_repo_scan_stubs(monkeypatch)
    repo_scan = importlib.import_module('gittensor.validator.issue_discovery.repo_scan')

    closed_issue = {
        'number': 7,
        'title': 'pre-inactive issue',
        'user': {'id': 101, 'login': 'miner'},
        'state': 'closed',
        'state_reason': 'completed',
        'created_at': '2026-04-01T00:00:00Z',
        'closed_at': '2026-04-10T00:00:00Z',
    }

    monkeypatch.setattr(repo_scan, '_fetch_closed_issues', lambda *_args, **_kwargs: [closed_issue])
    monkeypatch.setattr(repo_scan, 'find_solver_from_cross_references', lambda *_args, **_kwargs: (None, None))

    miner_eval = SimpleNamespace(github_id='101', merged_pull_requests=[], open_pull_requests=[], closed_pull_requests=[])
    result = asyncio.run(
            repo_scan.scan_closed_issues(
                miner_evaluations={1: miner_eval},
                master_repositories={'owner/repo': SimpleNamespace(weight=1.0, inactive_at='2026-04-15T00:00:00Z')},
                validator_pat='token',
            )
        )

    assert '101' in result
    assert [issue.number for issue in result['101']] == [7]
