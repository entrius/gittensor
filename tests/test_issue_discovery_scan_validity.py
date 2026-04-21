# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for scan-path issue discovery validity accounting."""

import importlib
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace


def _install_scoring_stubs(monkeypatch):
    fake_bt = SimpleNamespace(logging=SimpleNamespace(info=lambda *_args, **_kwargs: None))
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)

    fake_classes = ModuleType('gittensor.classes')
    fake_classes.Issue = object
    fake_classes.MinerEvaluation = object
    monkeypatch.setitem(sys.modules, 'gittensor.classes', fake_classes)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.issue_discovery.scoring', raising=False)


def test_scan_solved_issue_does_not_increment_valid_solved_count(monkeypatch):
    _install_scoring_stubs(monkeypatch)

    scoring = importlib.import_module('gittensor.validator.issue_discovery.scoring')
    data = {'discoverer': scoring._DiscovererData()}
    issue = SimpleNamespace(
        author_github_id='discoverer',
        state_reason='COMPLETED',
        state='CLOSED',
        closed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        number=17,
    )

    scoring._merge_scan_issues(
        scan_issues={'discoverer': [issue]},
        github_id_to_uid={'discoverer': 1},
        discoverer_data=data,
    )

    assert data['discoverer'].solved_count == 1
    assert data['discoverer'].valid_solved_count == 0
