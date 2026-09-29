# The MIT License (MIT)
# Copyright © 2026 Entrius

"""Head to head on paired scores: the crown needs every challenger seed valid and the bootstrap bound over the margin;
a king that scored nothing leaves no ratio; the solver hash sees paths, bytes and the executable bit."""

import json

import pytest

from gittensor.challenges.head_to_head import Entry, canonical, report, solver_sha
from gittensor.challenges.runner import SeedResult
from tests.challenges import fake_challenge

KING = [0.8, 1.0, 1.2, 0.9, 1.1, 1.0, 0.7, 1.3] * 4


def entry(scores: list[float], invalid: tuple[int, ...] = ()) -> Entry:
    return Entry('0' * 64, [SeedResult(i not in invalid, s, 0.0) for i, s in enumerate(scores)])


def judge(challenger: Entry, king: Entry) -> dict:
    return json.loads(canonical(report('fake', fake_challenge, 'small', '00ff', 0.01, challenger, king)))


def test_a_clearly_better_challenger_takes_the_crown():
    verdict = judge(entry([s * 1.05 for s in KING]), entry(KING))

    assert verdict['mean_gain'] == pytest.approx(0.05) and verdict['lower_99'] == pytest.approx(0.05)
    assert verdict['crown']


def test_one_invalid_challenger_seed_costs_the_crown_whatever_the_gain():
    scores = [s * 1.5 for s in KING]
    scores[3] = 0.0

    verdict = judge(entry(scores, invalid=(3,)), entry(KING))

    assert verdict['lower_99'] >= 0.01 and not verdict['crown']


def test_a_king_that_scored_nothing_leaves_no_ratio_and_any_valid_score_crowns():
    verdict = judge(entry(KING), entry([0.0] * len(KING), invalid=tuple(range(len(KING)))))

    assert (verdict['mean_gain'], verdict['lower_99'], verdict['crown']) == (None, None, True)


def test_solver_sha_covers_paths_bytes_and_the_executable_bit(tmp_path):
    (tmp_path / 'lib').mkdir()
    (tmp_path / 'lib' / 'a.py').write_text('x = 1\n')
    (tmp_path / 'solve').write_text('#!/bin/sh\n')
    before = solver_sha(tmp_path)

    (tmp_path / 'solve').chmod(0o755)
    executable = solver_sha(tmp_path)
    (tmp_path / 'lib' / 'a.py').rename(tmp_path / 'lib' / 'b.py')

    assert len({before, executable, solver_sha(tmp_path)}) == 3
