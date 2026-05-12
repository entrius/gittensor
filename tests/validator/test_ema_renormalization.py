"""Tests for EMA score corruption fix (#1177)."""

from types import SimpleNamespace

import numpy as np


def _make_validator():
    """Create a minimal validator-like object with scores for testing update_scores."""
    from neurons.validator import Validator

    v = object.__new__(Validator)
    v.scores = np.array([0.2, 0.5, 0.3], dtype=np.float32)
    setattr(v, 'config', SimpleNamespace(neuron=SimpleNamespace(moving_average_alpha=0.1)))
    return v


class TestUpdateScoresRenormalization:
    def test_blacklist_zeros_uids_without_renormalization(self):
        """Blacklisting zeros out UIDs but does NOT renormalize remaining scores to sum=1."""
        v = _make_validator()
        v.update_scores(np.array([0.1, 0.2, 0.3]), {0, 1, 2}, blacklisted_uids=[1])

        assert v.scores[1] == 0.0
        assert v.scores.sum() < 0.999
        assert v.scores[0] > 0
