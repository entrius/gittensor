# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Regression test for #1606: load_state() restored self.scores/self.hotkeys
from state.npz with no length validation against the live metagraph, so a
validator restart after a subnet resize left self.scores out of sync with
self.metagraph.n. resync_metagraph() had the same gap for shrinks (it only
special-cased growth).
"""

from unittest.mock import MagicMock

import numpy as np

from neurons.base.validator import BaseValidatorNeuron


class _FakeMetagraph:
    def __init__(self, hotkeys: list):
        self.hotkeys = hotkeys
        self.n = len(hotkeys)


class _DummyValidator(BaseValidatorNeuron):
    """Skips BaseValidatorNeuron.__init__ (wallet/subtensor/config setup) —
    only the attributes _realign_state_to_metagraph/load_state touch are
    set, but subclassing (rather than duck-typing) keeps `self.<method>()`
    calls inside load_state resolving correctly."""

    def __init__(self, scores: np.ndarray, hotkeys: list, metagraph_hotkeys: list):
        self.scores = scores
        self.hotkeys = hotkeys
        self.metagraph = _FakeMetagraph(metagraph_hotkeys)
        self.config = MagicMock()

    async def forward(self):
        """Unused abstract method stub — required to instantiate BaseNeuron."""
        raise NotImplementedError


def _realign(validator: _DummyValidator) -> None:
    validator._realign_state_to_metagraph()


class TestRealignStateToMetagraph:
    def test_growth_preserves_overlapping_scores_and_pads_with_zeros(self):
        validator = _DummyValidator(
            scores=np.array([1.0, 2.0, 3.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2'],
            metagraph_hotkeys=['hk0', 'hk1', 'hk2', 'hk3', 'hk4'],
        )

        _realign(validator)

        assert validator.scores.shape == (5,)
        assert list(validator.scores) == [1.0, 2.0, 3.0, 0.0, 0.0]
        assert validator.hotkeys == ['hk0', 'hk1', 'hk2', 'hk3', 'hk4']

    def test_shrink_truncates_without_crashing(self):
        # This is the case resync_metagraph previously dropped entirely: it
        # only resized self.scores when the metagraph grew.
        validator = _DummyValidator(
            scores=np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2', 'hk3', 'hk4'],
            metagraph_hotkeys=['hk0', 'hk1', 'hk2'],
        )

        _realign(validator)

        assert validator.scores.shape == (3,)
        assert list(validator.scores) == [1.0, 2.0, 3.0]
        assert validator.hotkeys == ['hk0', 'hk1', 'hk2']

    def test_replaced_hotkey_within_overlap_is_zeroed(self):
        validator = _DummyValidator(
            scores=np.array([1.0, 2.0, 3.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2'],
            metagraph_hotkeys=['hk0', 'new-hk1', 'hk2'],
        )

        _realign(validator)

        assert list(validator.scores) == [1.0, 0.0, 3.0]

    def test_same_size_no_replacement_is_a_no_op(self):
        validator = _DummyValidator(
            scores=np.array([1.0, 2.0, 3.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2'],
            metagraph_hotkeys=['hk0', 'hk1', 'hk2'],
        )

        _realign(validator)

        assert list(validator.scores) == [1.0, 2.0, 3.0]


class TestLoadStateRealignsToMetagraph:
    def test_restart_after_metagraph_growth_does_not_desync(self, tmp_path):
        # Reproduces the issue's repro steps: persist state for a smaller
        # metagraph, then "restart" against a grown live metagraph.
        full_path = tmp_path
        validator = _DummyValidator(
            scores=np.array([1.0, 2.0, 3.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2'],
            metagraph_hotkeys=['hk0', 'hk1', 'hk2', 'hk3', 'hk4'],
        )
        validator.config.neuron.full_path = str(full_path)
        validator.step = 0

        np.savez(
            str(full_path / 'state.npz'),
            step=7,
            scores=np.array([1.0, 2.0, 3.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2'],
        )

        validator.load_state()

        assert validator.step == 7
        assert validator.scores.shape == (5,)
        assert list(validator.scores) == [1.0, 2.0, 3.0, 0.0, 0.0]
        assert validator.hotkeys == ['hk0', 'hk1', 'hk2', 'hk3', 'hk4']

    def test_restart_after_metagraph_shrink_does_not_index_out_of_range(self, tmp_path):
        full_path = tmp_path
        validator = _DummyValidator(
            scores=np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2', 'hk3', 'hk4'],
            metagraph_hotkeys=['hk0', 'hk1', 'hk2'],
        )
        validator.config.neuron.full_path = str(full_path)
        validator.step = 0

        np.savez(
            str(full_path / 'state.npz'),
            step=3,
            scores=np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2', 'hk3', 'hk4'],
        )

        validator.load_state()

        assert validator.scores.shape == (3,)
        assert list(validator.scores) == [1.0, 2.0, 3.0]

        # The bug's actual failure mode: a subsequent scoring step indexing
        # UIDs up to the live metagraph size must not go out of bounds.
        uids_array = np.arange(validator.metagraph.n)
        scattered = np.zeros_like(validator.scores)
        scattered[uids_array] = 1.0  # would raise IndexError pre-fix
        assert scattered.shape == (3,)

    def test_missing_state_file_leaves_fresh_state_untouched(self, tmp_path):
        validator = _DummyValidator(
            scores=np.zeros(3, dtype=np.float32),
            hotkeys=['hk0', 'hk1', 'hk2'],
            metagraph_hotkeys=['hk0', 'hk1', 'hk2'],
        )
        validator.config.neuron.full_path = str(tmp_path)
        validator.step = 0

        validator.load_state()

        assert validator.scores.shape == (3,)
        assert list(validator.scores) == [0.0, 0.0, 0.0]
