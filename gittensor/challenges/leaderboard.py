# The MIT License (MIT)
# Copyright © 2026 Entrius

"""King of the hill per challenge. The king is found by walking the evaluated submissions in commit order: each takes
the crown only when its score beats the king's by the margin (``score > king * (1 + margin)``), so within the margin the
earlier commit keeps it, whatever order the submissions were evaluated in, and a re-evaluation (the same hotkey and
sha256) that drops a king's score hands the crown to whoever is best without it. A score of 0 never reigns. Persisted as
JSON in the scorecard's ``challenges`` shape."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Submission:
    hotkey: str
    submission_sha256: str
    score: float
    commit_block: int
    cpus: int = 0  # CPUs the solver was pinned to; 0 when it never ran


@dataclass
class Standing:
    king: Submission | None = None
    evaluated: list[Submission] = field(default_factory=list)


def crown(evaluated: list[Submission], margin: float) -> Submission | None:
    king = None  # same-block commits in a fixed order, so the evaluation order never matters
    for challenger in sorted(evaluated, key=lambda s: (s.commit_block, s.submission_sha256, s.hotkey)):
        if challenger.score > 0 and (king is None or challenger.score > king.score * (1 + margin)):
            king = challenger
    return king


class Leaderboard:
    def __init__(self, challenges: dict[str, Standing] | None = None):
        self.challenges = challenges or {}

    @classmethod
    def load(cls, path: str | Path) -> Leaderboard:
        path = Path(path)
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls(
            {
                cid: Standing(Submission(**s['king']) if s['king'] else None, [Submission(**e) for e in s['evaluated']])
                for cid, s in raw['challenges'].items()
            }
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(self.as_dict(), indent=1, sort_keys=True))
        tmp.replace(path)

    def as_dict(self) -> dict:
        return {'challenges': {cid: asdict(s) for cid, s in sorted(self.challenges.items())}}

    def offer(self, challenge_id: str, submission: Submission, margin: float) -> bool:
        """Record an evaluated submission (a re-evaluation replaces its entry); True when it holds the crown."""
        standing = self.challenges.setdefault(challenge_id, Standing())
        key = (submission.hotkey, submission.submission_sha256)
        standing.evaluated = [e for e in standing.evaluated if (e.hotkey, e.submission_sha256) != key]
        standing.evaluated.append(submission)
        standing.king = crown(standing.evaluated, margin)
        return standing.king == submission
