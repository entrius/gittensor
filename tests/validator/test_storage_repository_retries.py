from datetime import datetime, timezone
from types import ModuleType
import sys

import gittensor.validator.storage.repository as repository_module
from gittensor.classes import MinerEvaluation
from gittensor.validator.storage.repository import BaseRepository, Repository


class OperationalError(Exception):
    pass


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def execute(self, query, params=()):
        self.db.execute_calls += 1
        if self.db.fail_execute_times > 0:
            self.db.fail_execute_times -= 1
            raise OperationalError('temporary execute failure')

    def close(self):
        self.db.closed_cursors += 1


class FakeDB:
    def __init__(self, fail_execute_times=0):
        self.fail_execute_times = fail_execute_times
        self.execute_calls = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_execute_command_retries_transient_errors(monkeypatch):
    db = FakeDB(fail_execute_times=1)
    repo = BaseRepository(db)

    monkeypatch.setattr(repository_module.time, 'sleep', lambda _: None)

    assert repo.execute_command('UPDATE test SET value = %s', ('ok',)) is True
    assert db.execute_calls == 2
    assert db.rollbacks == 1
    assert db.commits == 1


def test_set_miner_evaluation_retries_execute_values(monkeypatch):
    db = FakeDB()
    repo = Repository(db)
    evaluation = MinerEvaluation(uid=1, hotkey='hotkey_1', github_id='1')
    evaluation.evaluation_timestamp = datetime.now(timezone.utc)

    call_state = {'calls': 0}

    def fake_execute_values(cursor, query, values, template=None, page_size=None):
        call_state['calls'] += 1
        if call_state['calls'] == 1:
            raise OperationalError('temporary bulk failure')

    psycopg2_module = ModuleType('psycopg2')
    extras_module = ModuleType('psycopg2.extras')
    extras_module.execute_values = fake_execute_values
    psycopg2_module.extras = extras_module

    monkeypatch.setattr(repository_module.time, 'sleep', lambda _: None)
    monkeypatch.setitem(sys.modules, 'psycopg2', psycopg2_module)
    monkeypatch.setitem(sys.modules, 'psycopg2.extras', extras_module)

    assert repo.set_miner_evaluation(evaluation) is True
    assert call_state['calls'] == 2
    assert db.rollbacks == 1
    assert db.commits == 1
