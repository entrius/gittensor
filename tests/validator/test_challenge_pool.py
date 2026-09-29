# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The validator side of the challenge pool: a valid challenge scorecard pays its registered hotkeys
CHALLENGE_EMISSION_SHARE carved out of the OSS pool; one that read_scorecard refuses (here: the compute schema) recycles
the whole share; with the share at 0 or no challenge pool the blend is bit-identical to today's."""

from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from gittensor.constants import OSS_EMISSION_SHARE, RECYCLE_UID
from gittensor.controller.pay.scorecard import SCHEMA as COMPUTE_SCHEMA
from gittensor.controller.pay.scorecard import write_scorecard
from gittensor.validator import emission_allocation
from gittensor.validator.challenge_pool import SCHEMA, challenge_pool_for
from gittensor.validator.compute_pool import ScorecardPool
from gittensor.validator.emission_allocation import blend_emission_pools
from tests.controller.test_scorecard import HK_A, HK_B, ISSUED
from tests.validator.test_blend_emission_pools import _config, _discovered_issue, _evaluation, _scored_pr

SHARE = 0.05


def scorecard_doc(schema: str = SCHEMA) -> dict:
    return {
        'schema': schema,
        'issued_at': ISSUED,
        'valid_until': ISSUED + 1_200,
        'recycle_share': 0.25,
        'hotkeys': [
            {'hotkey': HK_A, 'weight': 0.5},
            {'hotkey': HK_B, 'weight': 0.2},
            {'hotkey': 'unregistered', 'weight': 0.05},
        ],
        'challenges': {
            'routing-cvrptw': {
                'king': {'hotkey': HK_A, 'submission_sha256': 'ab' * 32, 'score': 1.12, 'commit_block': 100},
                'evaluated': [],
            }
        },
        'attestation': {'kind': 'dev'},
    }


def validator() -> Any:
    return cast(Any, SimpleNamespace(metagraph=SimpleNamespace(hotkeys=['recycle', HK_A, HK_B, 'oss-miner'])))


def test_a_valid_scorecard_pays_its_registered_hotkeys_out_of_the_oss_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(emission_allocation, 'CHALLENGE_EMISSION_SHARE', SHARE)
    path, sha = write_scorecard(tmp_path, scorecard_doc())
    pool = challenge_pool_for(validator(), str(path), now=ISSUED + 60)
    assert pool.sha256 == sha and pool.rewards == {1: 0.5, 2: 0.2}  # 'unregistered' has no UID

    repos = {'r/one': _config(emission_share=0.5, issue_discovery_share=0.0)}
    evaluations = {3: _evaluation(3, prs=[_scored_pr('r/one', 1, earned_score=10.0)])}
    rewards = blend_emission_pools(evaluations, repos, {RECYCLE_UID, 1, 2, 3}, challenge_pool=pool)

    oss = OSS_EMISSION_SHARE - SHARE
    assert rewards[1] == pytest.approx(SHARE * 0.5) and rewards[2] == pytest.approx(SHARE * 0.2)
    assert rewards[3] == pytest.approx(0.5 * oss)
    assert rewards[0] == pytest.approx(0.5 * oss + (1 - OSS_EMISSION_SHARE) + SHARE * 0.3)
    assert float(np.sum(rewards)) == pytest.approx(1.0)


def test_a_wrong_schema_scorecard_is_refused_and_recycles_the_challenge_share(tmp_path, monkeypatch):
    monkeypatch.setattr(emission_allocation, 'CHALLENGE_EMISSION_SHARE', SHARE)
    path, _ = write_scorecard(tmp_path, scorecard_doc(COMPUTE_SCHEMA))
    pool = challenge_pool_for(validator(), str(path), now=ISSUED + 60)
    assert pool.sha256 is None and pool.rewards == {} and f'not a {SCHEMA}' in pool.reason
    rewards = blend_emission_pools({}, {}, {RECYCLE_UID, 1, 2}, challenge_pool=pool)
    assert rewards.tolist() == [pytest.approx(1.0), 0.0, 0.0]  # never last-known weights


def test_with_a_zero_share_or_no_challenge_pool_the_blend_is_bit_identical(tmp_path):
    repos = {
        'r/one': _config(emission_share=0.37, issue_discovery_share=0.3, maintainer_cut=0.2),
        'r/two': _config(emission_share=0.21, issue_discovery_share=0.0),
    }
    evaluations = {
        1: _evaluation(1, prs=[_scored_pr('r/one', 1, earned_score=7.3)], issues=[_discovered_issue('r/one', 2, 1.9)]),
        2: _evaluation(2, prs=[_scored_pr('r/two', 3, earned_score=3.1), _scored_pr('r/one', 4, earned_score=2.2)]),
        3: _evaluation(3),
    }
    uids = {RECYCLE_UID, 1, 2, 3}
    args = (evaluations, repos, uids, {'r/one': [3]})
    compute = ScorecardPool({2: 0.43, 3: 0.19}, 'cd' * 32)
    path, _ = write_scorecard(tmp_path, scorecard_doc())
    challenge = challenge_pool_for(validator(), str(path), now=ISSUED + 60)
    assert challenge.rewards

    for compute_pool in (None, compute):
        today = blend_emission_pools(*args, compute_pool=compute_pool)
        assert np.array_equal(today, blend_emission_pools(*args, compute_pool=compute_pool, challenge_pool=challenge))
