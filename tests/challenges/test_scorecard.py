# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The scorecard's split of the pool, summed exactly, and its attestation key."""

import pytest

from gittensor.challenges.attest import DevAttestor, PolarisAttestor
from gittensor.challenges.leaderboard import Leaderboard, Submission
from gittensor.challenges.registry import Challenge
from gittensor.challenges.scorecard import build_scorecard


def test_one_king_of_every_funded_challenge_is_paid_exactly_the_whole_pool():
    registry = {c: Challenge(c, 'm', 'r', 'v', 't', 1, 0.01, s) for c, s in (('a', 0.1), ('b', 0.2), ('c', 0.7))}
    board = Leaderboard()
    for c in registry:
        board.offer(c, Submission('hk', 'x' * 64, 1.0, 1), 0.01)

    doc = build_scorecard(board, registry, 0.0)

    assert (doc['hotkeys'], doc['recycle_share']) == ([{'hotkey': 'hk', 'weight': 1.0}], 0.0)


def test_the_dev_key_is_kept_and_polaris_is_not_wired(tmp_path):
    assert DevAttestor(tmp_path / 'key').public_key == DevAttestor(tmp_path / 'key').public_key
    assert (tmp_path / 'key').stat().st_mode & 0o777 == 0o600
    with pytest.raises(NotImplementedError, match='docs.fr0ntierx.com'):
        PolarisAttestor().attest({})
