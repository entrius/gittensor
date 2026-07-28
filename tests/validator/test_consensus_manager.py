# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for snapshot lifecycle: warm compute, numerator cache, fallback ladder."""

import json
from types import SimpleNamespace

from gittensor.validator.utils.config import (
    CONSENSUS_CACHE_KEEP,
    CONSENSUS_MIN_VALIDATOR_STAKE_RAO,
    CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS,
)
from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.manager import ConsensusManager, run_weight_consensus

INTERVAL = CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS
STAKE = CONSENSUS_MIN_VALIDATOR_STAKE_RAO


class FakeBackend:
    """Serves canned baskets/stakes per snapshot; optionally unreachable."""

    def __init__(self, voters, reachable: bool = True):
        # voters: list of (hotkey, stake_rao, permit, structured-basket-or-None)
        self.voters = voters
        self.reachable = reachable
        self.fetch_calls = 0
        self.published = []

    def _check(self):
        if not self.reachable:
            raise RuntimeError('contract unreachable')

    def fetch_registry(self, snapshot):
        self._check()
        return {}

    def fetch_baskets(self, snapshot):
        self._check()
        self.fetch_calls += 1
        return {hotkey: prefs for hotkey, _, _, prefs in self.voters if prefs is not None}

    def fetch_own_basket(self):
        self._check()
        return None

    def publish_basket(self, prefs):
        self._check()
        self.published.append(prefs)
        return True

    def fetch_stakes_and_permits(self, snapshot):
        self._check()
        return (
            {hotkey: stake for hotkey, stake, _, _ in self.voters},
            {hotkey: permit for hotkey, _, permit, _ in self.voters},
        )


def _voters():
    return [
        ('hk1', 3 * STAKE, True, {'a/b': 65535}),
        ('hk2', STAKE, True, {'c/d': 65535}),
    ]


class TestConsensusManager:
    def test_fresh_compute_persists_and_reload_is_byte_identical(self, tmp_path):
        manager = ConsensusManager(backend=FakeBackend(_voters()), cache_dir=tmp_path)
        shares = manager.get_shares(INTERVAL + 10)

        reloaded = ConsensusManager(backend=FakeBackend([], reachable=False), cache_dir=tmp_path)
        assert json.dumps(reloaded.get_shares(INTERVAL + 50)) == json.dumps(shares)

    def test_cache_hit_makes_no_backend_calls(self, tmp_path):
        backend = FakeBackend(_voters())
        manager = ConsensusManager(backend=backend, cache_dir=tmp_path)
        manager.maybe_refresh(INTERVAL)
        calls = backend.fetch_calls
        manager.maybe_refresh(INTERVAL + 5)
        assert manager.get_shares(INTERVAL + 10) is not None
        assert backend.fetch_calls == calls

    def test_unreachable_backend_falls_back_to_last_good(self, tmp_path):
        backend = FakeBackend(_voters())
        manager = ConsensusManager(backend=backend, cache_dir=tmp_path)
        manager.maybe_refresh(INTERVAL)
        backend.reachable = False
        assert manager.get_shares(2 * INTERVAL + 10) == {'a/b': 0.75, 'c/d': 0.25}

    def test_unreachable_backend_no_cache_returns_none(self, tmp_path):
        manager = ConsensusManager(backend=FakeBackend([], reachable=False), cache_dir=tmp_path)
        assert manager.get_shares(INTERVAL + 10) is None

    def test_gate_failed_interval_cached_and_returns_none(self, tmp_path):
        voters = [('hk1', 3 * STAKE, True, None), ('hk2', STAKE, True, {'a/b': 65535})]
        backend = FakeBackend(voters)
        manager = ConsensusManager(backend=backend, cache_dir=tmp_path)
        assert manager.get_shares(INTERVAL) is None
        calls = backend.fetch_calls
        assert manager.get_shares(INTERVAL + 1) is None
        assert backend.fetch_calls == calls

    def test_cache_trims_and_tolerates_corrupt_file(self, tmp_path):
        (tmp_path / 'weight_consensus_cache.json').write_text('{corrupt')
        manager = ConsensusManager(backend=FakeBackend(_voters()), cache_dir=tmp_path)
        for i in range(CONSENSUS_CACHE_KEEP + 3):
            manager.maybe_refresh((i + 1) * INTERVAL)
        assert len(manager._cache) == CONSENSUS_CACHE_KEEP

    def test_maybe_refresh_retries_until_get_shares_marks_failed(self, tmp_path):
        backend = FakeBackend([], reachable=False)
        manager = ConsensusManager(backend=backend, cache_dir=tmp_path)
        manager.maybe_refresh(INTERVAL + 10)
        assert INTERVAL not in manager._failed_snapshots  # transient: retry next step
        assert manager.get_shares(INTERVAL + 20) is None
        assert INTERVAL in manager._failed_snapshots
        backend.reachable = True
        manager.maybe_refresh(INTERVAL + 30)
        assert backend.fetch_calls == 0  # marked failed: no more attempts this snapshot

    def test_store_hook_failure_does_not_break_compute(self, tmp_path):
        def broken_hook(*args):
            raise RuntimeError('db down')

        manager = ConsensusManager(backend=FakeBackend(_voters()), cache_dir=tmp_path, store_hook=broken_hook)
        assert manager.get_shares(INTERVAL) is not None

    def test_store_hook_receives_structured_baskets(self, tmp_path):
        seen = {}

        def hook(snapshot, baskets, stakes_rao, permits, result):
            seen.update(snapshot=snapshot, baskets=baskets, stakes=stakes_rao, permits=permits, result=result)

        manager = ConsensusManager(backend=FakeBackend(_voters()), cache_dir=tmp_path, store_hook=hook)
        manager.maybe_refresh(INTERVAL + 1)
        assert seen['snapshot'] == INTERVAL
        assert seen['baskets'] == {'hk1': {'a/b': 65535}, 'hk2': {'c/d': 65535}}
        assert seen['stakes']['hk1'] == 3 * STAKE
        assert seen['result'].voter_count == 2


class TestRunWeightConsensus:
    def _validator(self, tmp_path, backend):
        manager = ConsensusManager(backend=backend, cache_dir=tmp_path)
        return SimpleNamespace(
            config=SimpleNamespace(
                netuid=74,
                neuron=SimpleNamespace(
                    disable_weight_consensus=False, consensus_prefs_path=None, full_path=str(tmp_path)
                ),
            ),
            block=INTERVAL + 10,
            consensus_manager=manager,
        )

    def test_publishes_own_vote_and_overlays_aggregate(self, tmp_path):
        backend = FakeBackend(_voters())
        master = {'a/b': RepositoryConfig(emission_share=0.9), 'x/y': RepositoryConfig(emission_share=0.1)}
        result = run_weight_consensus(self._validator(tmp_path, backend), master)
        assert backend.published == [{'a/b': 58982, 'x/y': 6553}]
        assert result['a/b'].emission_share == 0.75
        assert result['x/y'].emission_share == 0.0
        assert result['c/d'].emission_share == 0.25

    def test_never_raises_and_falls_back_to_master(self, tmp_path):
        master = {'a/b': RepositoryConfig(emission_share=1.0)}
        validator = self._validator(tmp_path, FakeBackend([], reachable=False))
        assert run_weight_consensus(validator, master) is master

    def test_disable_flag_bypasses_everything(self, tmp_path):
        master = {'a/b': RepositoryConfig(emission_share=1.0)}
        validator = self._validator(tmp_path, FakeBackend(_voters()))
        validator.config.neuron.disable_weight_consensus = True
        assert run_weight_consensus(validator, master) is master

    def test_missing_manager_bypasses_everything(self, tmp_path):
        master = {'a/b': RepositoryConfig(emission_share=1.0)}
        validator = self._validator(tmp_path, FakeBackend(_voters()))
        validator.consensus_manager = None
        assert run_weight_consensus(validator, master) is master
