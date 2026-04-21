# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for validator emission blending."""

import importlib
import sys
from types import ModuleType, SimpleNamespace


class FakeArray:
    def __init__(self, values):
        self.values = [float(v) for v in values]

    def sum(self):
        return sum(self.values)

    def __mul__(self, scalar):
        return FakeArray([value * scalar for value in self.values])

    __rmul__ = __mul__

    def __iadd__(self, other):
        self.values = [left + right for left, right in zip(self.values, other.values)]
        return self

    def __getitem__(self, index):
        return self.values[index]

    def __setitem__(self, index, value):
        self.values[index] = float(value)

    def __iter__(self):
        return iter(self.values)


def _install_forward_stubs(monkeypatch):
    fake_bt = SimpleNamespace(logging=SimpleNamespace(info=lambda *_args, **_kwargs: None))
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)

    fake_np = ModuleType('numpy')
    fake_np.ndarray = FakeArray
    fake_np.array = lambda values: FakeArray(values)
    fake_np.zeros = lambda size: FakeArray([0.0] * size)
    monkeypatch.setitem(sys.modules, 'numpy', fake_np)

    fake_classes = ModuleType('gittensor.classes')
    fake_classes.MinerEvaluation = object
    monkeypatch.setitem(sys.modules, 'gittensor.classes', fake_classes)

    fake_uids = ModuleType('gittensor.utils.uids')
    fake_uids.get_all_uids = lambda _self: set()
    monkeypatch.setitem(sys.modules, 'gittensor.utils.uids', fake_uids)

    fake_issue_competitions = ModuleType('gittensor.validator.issue_competitions.forward')
    fake_issue_competitions.issue_competitions = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, 'gittensor.validator.issue_competitions.forward', fake_issue_competitions)

    fake_normalize = ModuleType('gittensor.validator.issue_discovery.normalize')
    fake_normalize.normalize_issue_discovery_rewards = lambda *_args, **_kwargs: {}
    monkeypatch.setitem(sys.modules, 'gittensor.validator.issue_discovery.normalize', fake_normalize)

    fake_repo_scan = ModuleType('gittensor.validator.issue_discovery.repo_scan')
    fake_repo_scan.scan_closed_issues = lambda *_args, **_kwargs: {}
    monkeypatch.setitem(sys.modules, 'gittensor.validator.issue_discovery.repo_scan', fake_repo_scan)

    fake_scoring = ModuleType('gittensor.validator.issue_discovery.scoring')
    fake_scoring.score_discovered_issues = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, 'gittensor.validator.issue_discovery.scoring', fake_scoring)

    fake_reward = ModuleType('gittensor.validator.oss_contributions.reward')
    fake_reward.get_rewards = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, 'gittensor.validator.oss_contributions.reward', fake_reward)

    fake_config = ModuleType('gittensor.validator.utils.config')
    fake_config.GITTENSOR_VALIDATOR_PAT = None
    fake_config.VALIDATOR_STEPS_INTERVAL = 1
    fake_config.VALIDATOR_WAIT = 1
    monkeypatch.setitem(sys.modules, 'gittensor.validator.utils.config', fake_config)

    fake_load_weights = ModuleType('gittensor.validator.utils.load_weights')
    fake_load_weights.RepositoryConfig = object
    fake_load_weights.load_master_repo_weights = lambda: {}
    fake_load_weights.load_programming_language_weights = lambda: {}
    fake_load_weights.load_token_config = lambda: SimpleNamespace(language_configs={})
    monkeypatch.setitem(sys.modules, 'gittensor.validator.utils.load_weights', fake_load_weights)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.forward', raising=False)


def test_blend_emission_pools_recycles_treasury_when_uid_absent(monkeypatch):
    _install_forward_stubs(monkeypatch)

    forward = importlib.import_module('gittensor.validator.forward')

    rewards = forward.blend_emission_pools(
        oss_rewards=FakeArray([0.0, 1.0]),
        issue_rewards=FakeArray([0.0, 0.0]),
        miner_uids={0, 5},
    )

    assert list(rewards) == [0.7, 0.3]
    assert sum(rewards) == 1.0
