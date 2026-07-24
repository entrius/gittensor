# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for snapshot lifecycle: warm compute, numerator cache, fallbacks."""

import json
from types import SimpleNamespace

from gittensor.validator.utils.config import (
    CONSENSUS_CACHE_KEEP,
    CONSENSUS_FRESH_WINDOW_BLOCKS,
    CONSENSUS_MIN_VALIDATOR_STAKE_RAO,
    CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS,
)
from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.codec import encode_prefs
from gittensor.validator.weight_consensus.manager import ConsensusManager, run_weight_consensus

INTERVAL = CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS
STAKE_ALPHA = CONSENSUS_MIN_VALIDATOR_STAKE_RAO / 1e9


class FakeSubtensor:
    """Serves canned commitments/metagraph per block; raises for pruned blocks."""

    def __init__(self, voters, prunable_before: int = -1):
        # voters: list of (hotkey, stake_alpha, permit, prefs-or-None)
        self.voters = voters
        self.prunable_before = prunable_before
        self.query_calls = 0

    def _check_pruned(self, block):
        if block < self.prunable_before:
            raise RuntimeError(f'State discarded for block {block}')

    def query_map(self, module, name, params, block):
        self._check_pruned(block)
        self.query_calls += 1
        return [
            (hotkey, {'info': {'fields': [[{'BigRaw': '0x' + encode_prefs(prefs).hex()}]]}})
            for hotkey, _, _, prefs in self.voters
            if prefs is not None
        ]

    def metagraph(self, netuid, block, lite):
        self._check_pruned(block)
        return SimpleNamespace(
            hotkeys=[hotkey for hotkey, _, _, _ in self.voters],
            S=[stake for _, stake, _, _ in self.voters],
            validator_permit=[permit for _, _, permit, _ in self.voters],
        )


def _voters():
    return [
        ('hk1', 3 * STAKE_ALPHA, True, {'a/b': 65535}),
        ('hk2', STAKE_ALPHA, True, {'c/d': 65535}),
    ]


class TestConsensusManager:
    def test_fresh_compute_persists_and_reload_is_byte_identical(self, tmp_path):
        subtensor = FakeSubtensor(_voters())
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        shares = manager.get_shares(subtensor, INTERVAL + 10)

        reloaded = ConsensusManager(netuid=74, cache_dir=tmp_path)
        assert json.dumps(reloaded.get_shares(FakeSubtensor([], prunable_before=10**9), INTERVAL + 50)) == json.dumps(
            shares
        )

    def test_cache_hit_makes_no_chain_calls(self, tmp_path):
        subtensor = FakeSubtensor(_voters())
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        manager.maybe_refresh(subtensor, INTERVAL)
        calls = subtensor.query_calls
        manager.maybe_refresh(subtensor, INTERVAL + 5)
        assert manager.get_shares(subtensor, INTERVAL + 10) is not None
        assert subtensor.query_calls == calls

    def test_pruned_state_falls_back_to_last_good(self, tmp_path):
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        manager.maybe_refresh(FakeSubtensor(_voters()), INTERVAL)
        pruned = FakeSubtensor(_voters(), prunable_before=10 * INTERVAL)
        shares = manager.get_shares(pruned, 2 * INTERVAL + CONSENSUS_FRESH_WINDOW_BLOCKS + 1)
        assert shares == {'a/b': 0.75, 'c/d': 0.25}

    def test_pruned_state_no_cache_returns_none(self, tmp_path):
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        assert manager.get_shares(FakeSubtensor([], prunable_before=10**9), INTERVAL + 10) is None

    def test_gate_failed_interval_cached_and_returns_none(self, tmp_path):
        voters = [('hk1', STAKE_ALPHA, True, None), ('hk2', STAKE_ALPHA / 3, True, {'a/b': 65535})]
        subtensor = FakeSubtensor(voters)
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        assert manager.get_shares(subtensor, INTERVAL) is None
        calls = subtensor.query_calls
        assert manager.get_shares(subtensor, INTERVAL + 1) is None
        assert subtensor.query_calls == calls

    def test_cache_trims_and_tolerates_corrupt_file(self, tmp_path):
        (tmp_path / 'weight_consensus_cache.json').write_text('{corrupt')
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        subtensor = FakeSubtensor(_voters())
        for i in range(CONSENSUS_CACHE_KEEP + 3):
            manager.maybe_refresh(subtensor, (i + 1) * INTERVAL)
        assert len(manager._cache) == CONSENSUS_CACHE_KEEP

    def test_maybe_refresh_retries_in_window_marks_failed_after(self, tmp_path):
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        pruned = FakeSubtensor([], prunable_before=10**9)
        manager.maybe_refresh(pruned, INTERVAL + 10)
        assert INTERVAL not in manager._failed_snapshots  # transient: retry next step
        manager.maybe_refresh(pruned, INTERVAL + CONSENSUS_FRESH_WINDOW_BLOCKS + 1)
        assert INTERVAL in manager._failed_snapshots

    def test_store_hook_failure_does_not_break_compute(self, tmp_path):
        def broken_hook(*args):
            raise RuntimeError('db down')

        manager = ConsensusManager(netuid=74, cache_dir=tmp_path, store_hook=broken_hook)
        assert manager.get_shares(FakeSubtensor(_voters()), INTERVAL) is not None


class TestRunWeightConsensus:
    def _validator(self, tmp_path, subtensor):
        manager = ConsensusManager(netuid=74, cache_dir=tmp_path)
        return SimpleNamespace(
            config=SimpleNamespace(
                netuid=74,
                neuron=SimpleNamespace(
                    disable_weight_consensus=False, consensus_prefs_path=None, full_path=str(tmp_path)
                ),
            ),
            subtensor=subtensor,
            wallet=SimpleNamespace(hotkey=SimpleNamespace(ss58_address='hk1')),
            block=INTERVAL + 10,
            consensus_manager=manager,
        )

    def test_never_raises_and_falls_back_to_master(self, tmp_path):
        class ExplodingSubtensor:
            def __getattr__(self, name):
                raise RuntimeError('chain down')

        master = {'a/b': RepositoryConfig(emission_share=1.0)}
        validator = self._validator(tmp_path, FakeSubtensor([], prunable_before=10**9))
        validator.subtensor = ExplodingSubtensor()
        assert run_weight_consensus(validator, master) is master

    def test_disable_flag_bypasses_everything(self, tmp_path):
        master = {'a/b': RepositoryConfig(emission_share=1.0)}
        validator = self._validator(tmp_path, FakeSubtensor(_voters()))
        validator.config.neuron.disable_weight_consensus = True
        assert run_weight_consensus(validator, master) is master
