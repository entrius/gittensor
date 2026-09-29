# The MIT License (MIT)
# Copyright © 2026 Entrius

"""King of the hill: the margin to dethrone, the earlier commit within it in either evaluation order, a re-evaluated
king, zero never reigns, and the board persists."""

import pytest

from gittensor.challenges.leaderboard import Leaderboard, Submission

MARGIN = 0.01


def sub(hotkey: str, score: float, block: int) -> Submission:
    return Submission(hotkey, hotkey * 8, score, block)


def test_a_challenger_dethrones_only_above_the_margin():
    board = Leaderboard()
    assert board.offer('c', sub('a', 1.0, 100), MARGIN)

    assert not board.offer('c', sub('b', 1.01, 200), MARGIN)
    assert board.offer('c', sub('d', 1.0101, 300), MARGIN)
    assert board.challenges['c'].king == sub('d', 1.0101, 300)


@pytest.mark.parametrize('order', [1, -1])
def test_within_the_margin_the_earlier_commit_reigns_whatever_the_evaluation_order(order):
    board = Leaderboard()
    for s in [sub('early', 1.0, 100), sub('late', 1.009, 200)][::order]:
        board.offer('c', s, MARGIN)

    assert board.challenges['c'].king == sub('early', 1.0, 100)


def test_a_king_re_evaluated_lower_hands_the_crown_to_the_best_without_it():
    board = Leaderboard()
    board.offer('c', sub('a', 1.0, 100), MARGIN)
    board.offer('c', sub('b', 0.9, 200), MARGIN)

    assert not board.offer('c', sub('a', 0.0, 100), MARGIN)
    assert board.challenges['c'].king == sub('b', 0.9, 200)


def test_a_zero_score_never_reigns():
    board = Leaderboard()

    assert not board.offer('c', sub('a', 0.0, 1), MARGIN)
    assert board.challenges['c'].king is None


def test_the_board_persists_and_reloads(tmp_path):
    board = Leaderboard()
    board.offer('c', sub('a', 1.0, 100), MARGIN)
    board.offer('c', sub('b', 0.5, 200), MARGIN)
    board.offer('c', sub('b', 0.7, 200), MARGIN)  # a re-evaluation replaces the entry
    path = tmp_path / 'state' / 'leaderboard.json'
    board.save(path)

    reloaded = Leaderboard.load(path)

    assert reloaded.challenges == board.challenges
    assert reloaded.challenges['c'].evaluated == [sub('a', 1.0, 100), sub('b', 0.7, 200)]
    assert Leaderboard.load(tmp_path / 'missing.json').challenges == {}
