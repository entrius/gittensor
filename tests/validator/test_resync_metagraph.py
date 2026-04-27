# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for BaseValidatorNeuron.resync_metagraph.

Locks the symmetric grow/shrink invariant: when the metagraph changes size in
either direction, self.hotkeys and self.scores must stay aligned with
self.metagraph.n, and the hotkey-replacement walk must not IndexError into a
shrunken metagraph.

Run tests:
    pytest tests/validator/test_resync_metagraph.py -v
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from neurons.base.validator import BaseValidatorNeuron


def _make_metagraph(hotkeys: list, axons_token: str = 'a') -> Any:
    """Return a stub metagraph object with the fields resync_metagraph touches.

    `axons` is a sentinel value used only for the inequality check at the top
    of resync_metagraph; differing tokens force the function to take the
    re-sync path. Return type is Any so pyright accepts the duck-typed stub
    where a bittensor.Metagraph is annotated.
    """
    return SimpleNamespace(
        hotkeys=list(hotkeys),
        n=len(hotkeys),
        axons=axons_token,
        sync=MagicMock(),  # called as self.metagraph.sync(subtensor=...) — no-op for tests
    )


def _make_validator_stub(prev_hotkeys: list, prev_scores: np.ndarray, new_hotkeys: list) -> Any:
    """Build a minimal stub that resync_metagraph can run against without __init__.

    self.metagraph starts with `new_hotkeys` already installed because we mock
    metagraph.sync to be a no-op — the test pre-stages the post-sync state.
    The pre-sync snapshot is recreated via copy.deepcopy in the function under
    test, so we must seed self.metagraph.axons distinct from the pre-sync axon
    token to force the function past its early-return guard.
    """
    stub = SimpleNamespace()
    stub.hotkeys = list(prev_hotkeys)
    stub.scores = prev_scores
    stub.subtensor = MagicMock()
    stub.metagraph = _make_metagraph(new_hotkeys, axons_token='post-sync')
    # Inject a pre-sync axons token by patching copy.deepcopy at the call site
    # is overkill — instead, just make the *current* metagraph's axons differ
    # from what deepcopy will capture. Since we pre-installed new_hotkeys with
    # axons='post-sync' and the real flow does `previous = deepcopy(self.metagraph)`
    # then `self.metagraph.sync(...)`, after sync the axons token is unchanged
    # in our stub. We force inequality by mutating it after the deepcopy step.
    # Simpler: stub metagraph.sync to swap the axons token.

    def _swap_axons_after_sync(*args, **kwargs):
        stub.metagraph.axons = 'post-sync-changed'

    stub.metagraph.sync = MagicMock(side_effect=_swap_axons_after_sync)
    stub.metagraph.axons = 'pre-sync'  # deepcopy captures this; sync flips it
    return stub


class TestResyncMetagraphGrow:
    """Pre-existing growth path — was uncovered by tests before this PR."""

    def test_grow_extends_scores_and_hotkeys(self):
        prev_hotkeys = ['a', 'b', 'c']
        prev_scores = np.array([0.1, 0.2, 0.3])
        new_hotkeys = ['a', 'b', 'c', 'd', 'e']
        stub = _make_validator_stub(prev_hotkeys, prev_scores, new_hotkeys)

        BaseValidatorNeuron.resync_metagraph(stub)

        assert stub.hotkeys == new_hotkeys
        assert stub.scores.shape == (5,)
        # Old slots preserved, new slots zeroed.
        assert stub.scores[0] == pytest.approx(0.1)
        assert stub.scores[1] == pytest.approx(0.2)
        assert stub.scores[2] == pytest.approx(0.3)
        assert stub.scores[3] == 0.0
        assert stub.scores[4] == 0.0


class TestResyncMetagraphShrink:
    """The bug this PR fixes: shrinkage was unhandled and IndexErrored."""

    def test_shrink_truncates_scores_and_hotkeys(self):
        prev_hotkeys = ['a', 'b', 'c', 'd', 'e']
        prev_scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
        new_hotkeys = ['a', 'b', 'c']
        stub = _make_validator_stub(prev_hotkeys, prev_scores, new_hotkeys)

        BaseValidatorNeuron.resync_metagraph(stub)

        assert stub.hotkeys == new_hotkeys
        assert stub.scores.shape == (3,)
        # Surviving slots preserved.
        assert stub.scores[0] == pytest.approx(0.1)
        assert stub.scores[1] == pytest.approx(0.2)
        assert stub.scores[2] == pytest.approx(0.3)

    def test_shrink_does_not_indexerror_on_replacement_walk(self):
        """The pre-fix loop iterated over old hotkeys and indexed new — IndexError
        on shrinkage. This test fails on the unfixed code with IndexError."""
        prev_hotkeys = ['a', 'b', 'c', 'd', 'e']
        prev_scores = np.array([0.0, 0.0, 0.0, 0.0, 0.0])
        new_hotkeys = ['a', 'b']  # smaller and last-shared slot unchanged
        stub = _make_validator_stub(prev_hotkeys, prev_scores, new_hotkeys)

        # Should not raise.
        BaseValidatorNeuron.resync_metagraph(stub)

        assert stub.hotkeys == new_hotkeys
        assert stub.scores.shape == (2,)

    def test_shrink_returns_independent_buffer_not_view(self):
        """Truncate must use .copy() so downstream in-place mutations on
        self.scores (e.g. blacklisted_uids zeroing in update_scores) cannot
        alias the larger original buffer through a numpy view."""
        prev_scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
        stub = _make_validator_stub(
            prev_hotkeys=['a', 'b', 'c', 'd', 'e'],
            prev_scores=prev_scores,
            new_hotkeys=['a', 'b', 'c'],
        )

        BaseValidatorNeuron.resync_metagraph(stub)

        # Mutate the (now-detached) original; the truncated array must not change.
        prev_scores[0] = 999.0
        assert stub.scores[0] == pytest.approx(0.1)


class TestResyncMetagraphReplacement:
    """Hotkey-replacement walk: zero scores at slots whose hotkey changed."""

    def test_replaced_hotkey_score_zeroed_within_shared_range(self):
        prev_hotkeys = ['a', 'b', 'c']
        prev_scores = np.array([0.1, 0.2, 0.3])
        new_hotkeys = ['a', 'X', 'c']  # uid=1 replaced
        stub = _make_validator_stub(prev_hotkeys, prev_scores, new_hotkeys)

        BaseValidatorNeuron.resync_metagraph(stub)

        assert stub.scores[0] == pytest.approx(0.1)
        assert stub.scores[1] == 0.0
        assert stub.scores[2] == pytest.approx(0.3)

    def test_replacement_walk_capped_at_shorter_length_on_shrink(self):
        """When metagraph shrinks AND a surviving slot was replaced, only the
        shared-range replacements zero out; the truncated tail is dropped
        regardless of replacement state."""
        prev_hotkeys = ['a', 'b', 'c', 'd']
        prev_scores = np.array([0.1, 0.2, 0.3, 0.4])
        new_hotkeys = ['a', 'X']  # uid=1 replaced; uid=2,3 dropped
        stub = _make_validator_stub(prev_hotkeys, prev_scores, new_hotkeys)

        BaseValidatorNeuron.resync_metagraph(stub)

        assert stub.hotkeys == new_hotkeys
        assert stub.scores.shape == (2,)
        assert stub.scores[0] == pytest.approx(0.1)
        assert stub.scores[1] == 0.0


class TestResyncMetagraphNoop:
    """Same-size, same-hotkeys path — early return on unchanged axons."""

    def test_unchanged_metagraph_returns_without_mutation(self):
        prev_hotkeys = ['a', 'b', 'c']
        prev_scores = np.array([0.1, 0.2, 0.3])

        stub: Any = SimpleNamespace()
        stub.hotkeys = list(prev_hotkeys)
        stub.scores = prev_scores.copy()
        stub.subtensor = MagicMock()
        stub.metagraph = _make_metagraph(prev_hotkeys, axons_token='same')
        # No side_effect — sync leaves axons unchanged, so equality check returns early.

        BaseValidatorNeuron.resync_metagraph(stub)

        # Untouched.
        assert stub.hotkeys == prev_hotkeys
        np.testing.assert_array_equal(stub.scores, prev_scores)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
