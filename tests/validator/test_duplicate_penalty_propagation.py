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

from gittensor.classes import Issue, MinerEvaluation
from gittensor.validator.oss_contributions.inspections import (
    detect_and_penalize_miners_sharing_github,
)
from neurons.base.validator import BaseValidatorNeuron


class _DummyValidator:
    def __init__(self, scores: np.ndarray, alpha: float = 0.1):
        self.scores = scores.astype(float, copy=True)
        self.config = MagicMock()
        self.config.neuron.moving_average_alpha = alpha


def _discovered_issue(uid: int) -> Issue:
    issue = Issue(
        number=uid,
        pr_number=uid + 100,
        repository_full_name='r/issue',
        title='cached',
        discovery_earned_score=10.0,
    )
    return issue


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


def test_duplicate_penalty_clears_cached_issue_discovery_rows():
    evaluations = {
        1: MinerEvaluation(uid=1, hotkey='hotkey_1', github_id='gh_shared'),
        2: MinerEvaluation(uid=2, hotkey='hotkey_2', github_id='gh_shared'),
        3: MinerEvaluation(uid=3, hotkey='hotkey_3', github_id='gh_honest'),
    }
    for evaluation in evaluations.values():
        evaluation.issue_discovery_score = 10.0
        evaluation.issue_discovery_issues = [_discovered_issue(evaluation.uid)]

    penalized_uids = detect_and_penalize_miners_sharing_github(evaluations)

    assert penalized_uids == {1, 2}
    for uid in penalized_uids:
        assert evaluations[uid].issue_discovery_score == 0.0
        assert evaluations[uid].issue_discovery_issues == []
    assert len(evaluations[3].issue_discovery_issues) == 1
