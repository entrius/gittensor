# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for validator storage transaction handling."""

import importlib
import sys
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))

    def close(self):
        return None


class FakeConnection:
    def __init__(self, autocommit=True):
        self.autocommit = autocommit
        self.commit_calls = 0
        self.rollback_calls = 0
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


@dataclass
class FakeMiner:
    uid: int
    hotkey: str
    github_id: str


@dataclass
class FakeMinerEvaluation:
    uid: int
    hotkey: str
    github_id: str = '123'
    merged_pull_requests: list = field(default_factory=lambda: [object()])
    open_pull_requests: list = field(default_factory=list)
    closed_pull_requests: list = field(default_factory=list)
    failed_reason: str | None = None
    base_total_score: float = 0.0
    total_score: float = 0.0
    total_collateral_score: float = 0.0
    total_nodes_scored: int = 0
    unique_repos_count: int = 0
    is_eligible: bool = False
    credibility: float = 0.0
    total_token_score: float = 0.0
    total_structural_count: int = 0
    total_structural_score: float = 0.0
    total_leaf_count: int = 0
    total_leaf_score: float = 0.0
    issue_discovery_score: float = 0.0
    issue_token_score: float = 0.0
    issue_credibility: float = 0.0
    is_issue_eligible: bool = False
    total_solved_issues: int = 0
    total_valid_solved_issues: int = 0
    total_closed_issues: int = 0
    total_open_issues: int = 0
    evaluation_timestamp: object = None

    def get_all_issues(self):
        return [object()]

    def get_all_file_changes(self):
        return []


class FakeRepo:
    def __init__(self, db_connection):
        self.db = db_connection

    def set_miner(self, _miner):
        return True

    def store_pull_requests_bulk(self, pull_requests):
        return 0 if pull_requests else 0

    def store_issues_bulk(self, issues):
        return 0 if issues else 0

    def store_file_changes_bulk(self, file_changes):
        return len(file_changes)

    def cleanup_stale_miner_data(self, _evaluation):
        return None

    def set_miner_evaluation(self, _evaluation):
        return True


def _install_common_stubs(monkeypatch):
    fake_bt = SimpleNamespace(logging=SimpleNamespace(error=lambda *_args, **_kwargs: None))
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)


def test_repository_skips_inner_commit_when_outer_transaction_active(monkeypatch):
    _install_common_stubs(monkeypatch)

    fake_numpy = ModuleType('numpy')
    fake_numpy.integer = int
    monkeypatch.setitem(sys.modules, 'numpy', fake_numpy)

    fake_classes = ModuleType('gittensor.classes')
    fake_classes.FileChange = object
    fake_classes.Issue = object
    fake_classes.Miner = FakeMiner
    fake_classes.MinerEvaluation = FakeMinerEvaluation
    fake_classes.PullRequest = object
    monkeypatch.setitem(sys.modules, 'gittensor.classes', fake_classes)

    fake_queries = ModuleType('gittensor.validator.storage.queries')
    for name in [
        'BULK_UPSERT_FILE_CHANGES',
        'BULK_UPSERT_ISSUES',
        'BULK_UPSERT_MINER_EVALUATION',
        'BULK_UPSERT_PULL_REQUESTS',
        'CLEANUP_STALE_MINER_EVALUATIONS',
        'CLEANUP_STALE_MINER_EVALUATIONS_BY_HOTKEY',
        'CLEANUP_STALE_MINERS',
        'CLEANUP_STALE_MINERS_BY_HOTKEY',
        'SET_MINER',
    ]:
        setattr(fake_queries, name, name)
    monkeypatch.setitem(sys.modules, 'gittensor.validator.storage.queries', fake_queries)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.storage.repository', raising=False)
    repository = importlib.import_module('gittensor.validator.storage.repository')

    db = FakeConnection(autocommit=False)
    repo = repository.Repository(db)

    assert repo.set_miner(FakeMiner(uid=1, hotkey='hk', github_id='123')) is True
    assert db.commit_calls == 0
    assert db.rollback_calls == 0


def test_store_evaluation_rolls_back_when_subwrite_returns_zero(monkeypatch):
    _install_common_stubs(monkeypatch)

    fake_classes = ModuleType('gittensor.classes')
    fake_classes.Miner = FakeMiner
    fake_classes.MinerEvaluation = FakeMinerEvaluation
    monkeypatch.setitem(sys.modules, 'gittensor.classes', fake_classes)

    fake_database = ModuleType('gittensor.validator.storage.database')
    fake_database.create_database_connection = lambda: FakeConnection()
    monkeypatch.setitem(sys.modules, 'gittensor.validator.storage.database', fake_database)

    fake_repository = ModuleType('gittensor.validator.storage.repository')
    fake_repository.Repository = FakeRepo
    monkeypatch.setitem(sys.modules, 'gittensor.validator.storage.repository', fake_repository)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.utils.storage', raising=False)
    storage = importlib.import_module('gittensor.validator.utils.storage')

    db_storage = storage.DatabaseStorage()
    result = db_storage.store_evaluation(FakeMinerEvaluation(uid=7, hotkey='hotkey'))

    assert result.success is False
    assert db_storage.db_connection.commit_calls == 0
    assert db_storage.db_connection.rollback_calls == 1
    assert result.errors
