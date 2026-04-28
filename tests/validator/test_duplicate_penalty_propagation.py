# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Regression test for #782: penalized_uids returned by
detect_and_penalize_miners_sharing_github must reach update_scores as
blacklisted_uids so prior EMA history for duplicate-account cheaters is
wiped instead of bleeding through alpha-blending.
"""

from typing import cast
from unittest.mock import MagicMock

import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.validator.oss_contributions.inspections import (
    detect_and_penalize_miners_sharing_github,
)
from neurons.base.validator import BaseValidatorNeuron


class _DummyValidator:
    def __init__(self, scores: np.ndarray, alpha: float = 0.1):
        self.scores = scores.astype(float, copy=True)
        self.config = MagicMock()
        self.config.neuron.moving_average_alpha = alpha


def test_detected_duplicates_wipe_prior_ema_via_update_scores():
    # Prior round: UID 1 and UID 2 posted PRs under the same GitHub account
    # and accumulated EMA weight. UID 0 is honest. On the unfixed call
    # pattern (no blacklisted_uids), cheater scores would decay only to
    # 0.9 * 0.45 = 0.405 via EMA instead of being zeroed.
    prior = np.array([0.1, 0.45, 0.45])
    validator = _DummyValidator(scores=prior, alpha=0.1)

    evaluations = {
        0: MinerEvaluation(uid=0, hotkey='hotkey_0', github_id='gh_honest'),
        1: MinerEvaluation(uid=1, hotkey='hotkey_1', github_id='gh_shared'),
        2: MinerEvaluation(uid=2, hotkey='hotkey_2', github_id='gh_shared'),
    }

    penalized_uids = detect_and_penalize_miners_sharing_github(evaluations)

    BaseValidatorNeuron.update_scores(
        cast(BaseValidatorNeuron, validator),
        np.zeros(3),
        {0, 1, 2},
        blacklisted_uids=sorted(penalized_uids),
    )

    assert validator.scores[1] == 0.0
    assert validator.scores[2] == 0.0
    assert validator.scores[0] > 0.0
    assert np.isclose(validator.scores.sum(), 1.0)
