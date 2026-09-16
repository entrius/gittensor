# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The validator side of the compute pool, with a fixture scorecard: a valid scorecard is signed, committed once per
sha256 (fake subtensor, no chain) and blended in as the compute share, the commit record written under the
validator's own state (never the controller's read-only scorecard directory); a stale, tampered or missing one
commits nothing and recycles the whole compute share; unregistered hotkeys are not paid; with no compute pool the
blend is unchanged."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import bittensor as bt
import numpy as np
import pytest

from gittensor.constants import OSS_EMISSION_SHARE, RECYCLE_UID
from gittensor.controller.pay.scorecard import write_scorecard
from gittensor.validator.compute_pool import (
    COMMIT_LOG,
    DEFAULT_COMMIT_DIR,
    ComputePool,
    commit_path_for,
    compute_pool_for,
    pool_from_scorecard,
)
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


def validator(hotkeys, subtensor, state_dir: Path | None = None) -> Any:
    fields: dict = {
        'metagraph': SimpleNamespace(hotkeys=hotkeys, netuid=74),
        'subtensor': subtensor,
        'wallet': SimpleNamespace(hotkey=FakeHotkey()),
    }
    if state_dir is not None:  # the neuron's own state directory (state.npz lives there)
        fields['config'] = SimpleNamespace(neuron=SimpleNamespace(full_path=str(state_dir)))
    return cast(Any, SimpleNamespace(**fields))


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / 'validator-state'


@pytest.fixture
def scorecard(tmp_path):
    doc = example_scorecard()
    doc['hotkeys'].append({**doc['hotkeys'][0], 'hotkey': 'unregistered', 'weight': 0.001})
    doc['recycle_share'] -= 0.001
    path, sha = write_scorecard(tmp_path / 'scorecard', doc)
    return path, sha, doc


def test_a_valid_scorecard_is_signed_committed_once_and_paid_as_the_compute_share(scorecard, state_dir):
    path, sha, doc = scorecard
    subtensor = FakeSubtensor()
    vali = validator(['recycle', HK_A, HK_B], subtensor, state_dir)
    pool = compute_pool_for(vali, str(path), now=ISSUED + 60)
    weight_a = next(h['weight'] for h in doc['hotkeys'] if h['hotkey'] == HK_A)
    assert 0 < weight_a < 1
    assert pool.sha256 == sha and pool.rewards == {1: weight_a}  # HK_B earns 0, 'unregistered' has no UID
    assert subtensor.commitments == [('5ValidatorHotkey', 74, f'gt-scorecard:{sha}')]
    log = json.loads((state_dir / COMMIT_LOG).read_text())
    assert log['committed'] and log['sha256'] == sha and log['signature'] == '0x' + (b'sig:' + sha.encode()[:8]).hex()
    assert not (path.parent / COMMIT_LOG).exists()  # the controller's directory is read-only input

    compute_pool_for(vali, str(path), now=ISSUED + 120)
    assert len(subtensor.commitments) == 1  # the same sha256 is not committed twice

    uids = {RECYCLE_UID, 1, 2}
    rewards = blend_emission_pools({}, {}, uids, None, compute_pool=pool)
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


def test_a_failed_commit_still_pays_and_is_retried_next_round(scorecard, state_dir):
    path, sha, _ = scorecard
    subtensor = FakeSubtensor(ok=False)
    vali = validator(['recycle', HK_A], subtensor, state_dir)
    assert compute_pool_for(vali, str(path), now=ISSUED + 60).rewards
    assert json.loads((state_dir / COMMIT_LOG).read_text())['error'] == 'rate limited'
    subtensor.ok = True
    compute_pool_for(vali, str(path), now=ISSUED + 120)
    assert len(subtensor.commitments) == 2 and getattr(vali, 'last_scorecard_sha256') == sha


def test_without_a_compute_pool_the_compute_share_recycles():
    # After the cutover there is no serving pool: with no scorecard, no repos and no compute pool, everything recycles.
    uids = {RECYCLE_UID, 1}
    today = blend_emission_pools({}, {}, uids, None)
    assert today[1] == pytest.approx(0.0)
    assert today[0] == pytest.approx(1.0)
    empty = pool_from_scorecard('/nonexistent/latest.json', [], ISSUED)
    assert isinstance(empty, ComputePool) and empty.sha256 is None and 'nonexistent' in empty.reason


def test_the_commit_record_goes_under_the_validators_own_state_never_the_read_only_scorecard_dir(
    scorecard, state_dir, tmp_path, monkeypatch
):
    # 9/16 soak: the validator wrote validator_commit.json into the controller's scorecard dir, mounted read-only
    path, sha, _ = scorecard
    warnings = []
    monkeypatch.setattr(bt.logging, 'warning', lambda message, *a, **k: warnings.append(str(message)))
    path.parent.chmod(0o500)  # read-only, as the controller's directory is on a shared host
    try:
        if os.access(path.parent, os.W_OK):
            pytest.skip('running as root: the directory cannot be made read-only')
        subtensor = FakeSubtensor()
        vali = validator(['recycle', HK_A], subtensor, state_dir)
        assert compute_pool_for(vali, str(path), now=ISSUED + 60).sha256 == sha
        assert len(subtensor.commitments) == 1 and warnings == []
        record = json.loads((state_dir / COMMIT_LOG).read_text())
        assert record['committed'] and record['sha256'] == sha
        assert not (path.parent / COMMIT_LOG).exists()

        elsewhere = tmp_path / 'elsewhere' / 'commit.json'  # COMPUTE_COMMIT_PATH wins over the neuron's state dir
        other = validator(['recycle', HK_A], FakeSubtensor(), state_dir)
        assert compute_pool_for(other, str(path), now=ISSUED + 60, commit_path=str(elsewhere)).sha256 == sha
        assert json.loads(elsewhere.read_text())['sha256'] == sha and warnings == []
    finally:
        path.parent.chmod(0o700)

    assert commit_path_for(vali) == state_dir / COMMIT_LOG
    assert commit_path_for(vali, '~/x/commit.json') == Path('~/x/commit.json').expanduser()
    assert commit_path_for(SimpleNamespace()) == Path(DEFAULT_COMMIT_DIR).expanduser() / COMMIT_LOG
