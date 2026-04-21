# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for solver selection from cross references."""

import importlib
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def _install_github_api_stubs(monkeypatch):
    fake_bt = SimpleNamespace(logging=SimpleNamespace(debug=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None))
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)

    fake_classes = ModuleType('gittensor.classes')
    fake_classes.FileChange = object
    fake_classes.MinerEvaluation = object

    class FakePRState(Enum):
        MERGED = 'MERGED'
        OPEN = 'OPEN'
        CLOSED = 'CLOSED'

    fake_classes.PRState = FakePRState
    monkeypatch.setitem(sys.modules, 'gittensor.classes', fake_classes)

    monkeypatch.delitem(sys.modules, 'gittensor.utils.github_api_tools', raising=False)


def test_find_solver_prefers_earliest_merged_pr(monkeypatch):
    _install_github_api_stubs(monkeypatch)
    github_api_tools = importlib.import_module('gittensor.utils.github_api_tools')

    prs = [
        {'number': 20, 'author_id': 200, 'state': 'MERGED', 'merged_at': '2025-06-15T00:00:00Z', 'closing_numbers': [12]},
        {'number': 10, 'author_id': 100, 'state': 'MERGED', 'merged_at': '2025-01-01T00:00:00Z', 'closing_numbers': [12]},
        {'number': 15, 'author_id': 150, 'state': 'MERGED', 'merged_at': '2025-03-01T00:00:00Z', 'closing_numbers': [12]},
    ]

    with patch.object(github_api_tools, '_search_issue_referencing_prs_graphql', return_value=prs):
        solver_id, pr_number = github_api_tools.find_solver_from_cross_references('owner/repo', 12, 'token')

    assert solver_id == 100
    assert pr_number == 10
