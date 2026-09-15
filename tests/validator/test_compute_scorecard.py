# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The validator side of the compute pool, with a fixture scorecard: a valid scorecard is signed, committed once per
sha256 (fake subtensor, no chain) and blended in as the compute share; a stale, tampered or missing one commits nothing
and recycles the whole compute share; unregistered hotkeys are not paid; with no compute pool the blend is unchanged."""

import json
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from gittensor.constants import OSS_EMISSION_SHARE, RECYCLE_UID, SERVING_EMISSION_SHARE_CAP
from gittensor.controller.pay.scorecard import write_scorecard
from gittensor.validator.compute_pool import COMMIT_LOG, ComputePool, compute_pool_for, pool_from_scorecard
from gittensor.validator.emission_allocation import blend_emission_pools
from tests.controller.test_scorecard import HK_A, HK_B, ISSUED, example_scorecard

COMPUTE_SHARE = 1.0 - OSS_EMISSION_SHARE


class FakeSubtensor:
    def __init__(self, ok=True):
        self.ok, self.commitments = ok, []

    def set_commitment(self, wallet, netuid, data):
        self.commitments.append((wallet.hotkey.ss58_address, netuid, data))
        return SimpleNamespace(success=self.ok, message='' if self.ok else 'rate limited')


class FakeHotkey:
    ss58_address = '5ValidatorHotkey'

    def sign(self, data: bytes) -> bytes:
        return b'sig:' + data[:8]


def validator(hotkeys, subtensor) -> Any:
    return cast(
        Any,
        SimpleNamespace(
            metagraph=SimpleNamespace(hotkeys=hotkeys, netuid=74),
            subtensor=subtensor,
            wallet=SimpleNamespace(hotkey=FakeHotkey()),
        ),
    )


@pytest.fixture
def scorecard(tmp_path):
    doc = example_scorecard()
    doc['hotkeys'].append({**doc['hotkeys'][0], 'hotkey': 'unregistered', 'weight': 0.001})
    doc['recycle_share'] -= 0.001
    path, sha = write_scorecard(tmp_path / 'scorecard', doc)
    return path, sha, doc


def test_a_valid_scorecard_is_signed_committed_once_and_paid_as_the_compute_share(scorecard):
    path, sha, doc = scorecard
    subtensor = FakeSubtensor()
    vali = validator(['recycle', HK_A, HK_B], subtensor)
    pool = compute_pool_for(vali, str(path), now=ISSUED + 60)
    weight_a = next(h['weight'] for h in doc['hotkeys'] if h['hotkey'] == HK_A)
    assert 0 < weight_a < 1
    assert pool.sha256 == sha and pool.rewards == {1: weight_a}  # HK_B earns 0, 'unregistered' has no UID
    assert subtensor.commitments == [('5ValidatorHotkey', 74, f'gt-scorecard:{sha}')]
    log = json.loads((path.parent / COMMIT_LOG).read_text())
    assert log['committed'] and log['sha256'] == sha and log['signature'] == '0x' + (b'sig:' + sha.encode()[:8]).hex()

    compute_pool_for(vali, str(path), now=ISSUED + 120)
    assert len(subtensor.commitments) == 1  # the same sha256 is not committed twice

    uids = {RECYCLE_UID, 1, 2}
    rewards = blend_emission_pools({}, {}, uids, None, {2: 5.0}, None, True, pool)  # serving scores are ignored
    assert rewards[1] == pytest.approx(COMPUTE_SHARE * weight_a)
    assert rewards[2] == 0.0
    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE + COMPUTE_SHARE * (1 - weight_a))  # no repos: OSS recycles
    assert float(np.sum(rewards)) == pytest.approx(1.0)


@pytest.mark.parametrize('spoil', ['stale', 'tampered', 'missing'])
def test_a_stale_tampered_or_missing_scorecard_commits_nothing_and_recycles_the_compute_share(scorecard, spoil):
    path, sha, doc = scorecard
    now = ISSUED + 60
    if spoil == 'stale':
        now = doc['valid_until'] + 1
    elif spoil == 'tampered':
        path.write_bytes(path.read_bytes().replace(b'"standing":"standard"', b'"standing":"trusted"'))
    else:
        path.unlink()
    subtensor = FakeSubtensor()
    pool = compute_pool_for(validator(['recycle', HK_A, HK_B], subtensor), str(path), now=now)
    assert pool.sha256 is None and pool.rewards == {} and subtensor.commitments == []
    rewards = blend_emission_pools({}, {}, {RECYCLE_UID, 1, 2}, compute_pool=pool)
    assert rewards[0] == pytest.approx(1.0) and rewards[1] == 0.0  # never last-known weights


def test_a_failed_commit_still_pays_and_is_retried_next_round(scorecard):
    path, sha, _ = scorecard
    subtensor = FakeSubtensor(ok=False)
    vali = validator(['recycle', HK_A], subtensor)
    assert compute_pool_for(vali, str(path), now=ISSUED + 60).rewards
    assert json.loads((path.parent / COMMIT_LOG).read_text())['error'] == 'rate limited'
    subtensor.ok = True
    compute_pool_for(vali, str(path), now=ISSUED + 120)
    assert len(subtensor.commitments) == 2 and getattr(vali, 'last_scorecard_sha256') == sha


def test_without_a_compute_pool_the_blend_is_todays():
    uids = {RECYCLE_UID, 1}
    today = blend_emission_pools({}, {}, uids, None, {1: 1.0}, None, True)  # the serving pool, paid as it is today
    assert today[1] == pytest.approx(SERVING_EMISSION_SHARE_CAP)
    assert today[0] == pytest.approx(1 - SERVING_EMISSION_SHARE_CAP)  # no repos: OSS and the slack recycle
    empty = pool_from_scorecard('/nonexistent/latest.json', [], ISSUED)
    assert isinstance(empty, ComputePool) and empty.sha256 is None and 'nonexistent' in empty.reason
