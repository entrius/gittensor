# The MIT License (MIT)
# Copyright © 2026 Entrius

"""One evaluator round: score each new submission on seeds from the round's public seed, offer it to its challenge's
leaderboard, persist the board, and write the attested scorecard. Finding submissions (chain commitments, Hippius
download, bundle sha256 check) is the caller's; so is choosing the public seed (a block hash after every commit). A
candidate that cannot be run (its challenge package missing or mismatched, its directory unreadable) scores 0 and is
logged; with a working sandbox the board and the scorecard are always written."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gittensor.challenges import runner
from gittensor.challenges.leaderboard import Leaderboard, Submission
from gittensor.challenges.registry import Challenge, import_challenge
from gittensor.challenges.scorecard import build_scorecard
from gittensor.controller.pay.scorecard import write_scorecard

log = logging.getLogger(__name__)


class SandboxUnavailable(RuntimeError):
    """bwrap does not run here, so no round can be scored."""


class Attestor(Protocol):
    def attest(self, doc: dict) -> dict: ...


@dataclass(frozen=True)
class Candidate:
    challenge_id: str
    hotkey: str
    commit_block: int
    submission_sha256: str
    solver_dir: Path


def run_round(
    registry: Mapping[str, Challenge],
    candidates: Iterable[Candidate],
    public_seed: str,
    board_path: str | Path,
    scorecard_dir: str | Path,
    attestor: Attestor,
    now: float,
) -> tuple[Path, str]:
    """The scorecard's path and sha256. Candidates for a challenge not in the registry are skipped. On a host without a
    working sandbox it raises ``SandboxUnavailable`` before touching the board: never zeros from a broken host."""
    if error := runner.sandbox_error():
        raise SandboxUnavailable(error)
    board = Leaderboard.load(board_path)
    for c in candidates:
        challenge = registry.get(c.challenge_id)
        if challenge is None:
            continue
        try:
            module = import_challenge(challenge)
            result = runner.evaluate(module, c.solver_dir, challenge.tier, public_seed, challenge.seeds)
            score, cpus = result.score, result.cpus
        except Exception as e:  # one bad candidate never costs the round
            log.warning(f'{c.challenge_id}: {c.hotkey} {c.submission_sha256[:16]} scores 0: {type(e).__name__}: {e}')
            score, cpus = 0.0, 0
        submission = Submission(c.hotkey, c.submission_sha256, score, c.commit_block, cpus)
        board.offer(c.challenge_id, submission, challenge.dethrone_margin)
    board.save(board_path)
    doc = build_scorecard(board, registry, now)
    doc['attestation'] = attestor.attest(doc)
    return write_scorecard(scorecard_dir, doc)
