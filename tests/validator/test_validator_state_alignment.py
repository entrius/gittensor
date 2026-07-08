# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for validator state alignment after metagraph size changes."""

from unittest.mock import MagicMock

import numpy as np

from neurons.base.validator import BaseValidatorNeuron


class _ValidatorStub:
    _align_scores_to_metagraph = BaseValidatorNeuron._align_scores_to_metagraph
    load_state = BaseValidatorNeuron.load_state

    metagraph: MagicMock
    config: MagicMock
    step: int
    scores: np.ndarray
    hotkeys: list[str]

    def __init__(self, n: int, hotkeys: list[str]):
        self.metagraph = MagicMock()
        self.metagraph.n = n
        self.metagraph.hotkeys = hotkeys


def test_align_scores_grows_with_metagraph():
    validator = _ValidatorStub(n=4, hotkeys=['h0', 'h1', 'h2', 'h3'])
    scores = np.array([0.1, 0.2, 0.3], dtype=np.float32)

    aligned = validator._align_scores_to_metagraph(scores, ['h0', 'h1', 'h2'])

    assert aligned.shape == (4,)
    assert np.allclose(aligned[:3], [0.1, 0.2, 0.3])
    assert aligned[3] == 0.0


def test_align_scores_shrinks_with_metagraph():
    validator = _ValidatorStub(n=2, hotkeys=['h0', 'h1'])
    scores = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)

    aligned = validator._align_scores_to_metagraph(scores, ['h0', 'h1', 'h2', 'h3'])

    assert aligned.shape == (2,)
    assert np.allclose(aligned, [0.1, 0.2])


def test_align_scores_zeros_replaced_hotkey():
    validator = _ValidatorStub(n=3, hotkeys=['h0', 'new_h1', 'h2'])
    scores = np.array([0.1, 0.5, 0.4], dtype=np.float32)

    aligned = validator._align_scores_to_metagraph(scores, ['h0', 'old_h1', 'h2'])

    assert np.allclose(aligned, [0.1, 0.0, 0.4])


def test_load_state_realigns_persisted_scores_to_current_metagraph(tmp_path, monkeypatch):
    validator = _ValidatorStub(n=4, hotkeys=['h0', 'h1', 'h2', 'h3'])
    validator.config = MagicMock()
    validator.config.neuron.full_path = str(tmp_path)
    state_path = tmp_path / 'state.npz'
    np.savez(
        state_path,
        step=42,
        scores=np.array([0.1, 0.2, 0.3], dtype=np.float32),
        hotkeys=['h0', 'h1', 'h2'],
    )

    validator.load_state()

    assert validator.step == 42
    assert validator.scores.shape == (4,)
    assert np.allclose(validator.scores[:3], [0.1, 0.2, 0.3])
    assert validator.scores[3] == 0.0
    assert validator.hotkeys == ['h0', 'h1', 'h2', 'h3']
