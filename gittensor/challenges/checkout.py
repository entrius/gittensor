# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A miner's clone of a challenge repo (G.1): ``.gittensor/challenge.json``, ``KING`` and ``solvers/<login>/<n>/``,
and the checks ``gitt challenge submit`` runs on ``attestation.json`` before opening the PR."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from gittensor.challenges.attestation import DEV, Attestation, verify

CONFIG = Path('.gittensor/challenge.json')
ATTESTATION = 'attestation.json'
OWNER = 'entrius'


def repo_name(challenge: str) -> str:
    """``intents`` -> ``entrius/gt-challenge-intents``; ``OWNER/NAME`` stays as is."""
    return challenge if '/' in challenge else f'{OWNER}/gt-challenge-{challenge}'


@dataclass(frozen=True)
class Checkout:
    root: Path
    config: dict

    @classmethod
    def find(cls, challenge: str | None = None) -> Checkout | None:
        """The checkout holding the working directory, else ``./<repo name>`` for ``challenge``."""
        here = Path.cwd().resolve()
        candidates = [here, *here.parents]
        if challenge:
            candidates.append(here / repo_name(challenge).split('/')[1])
        root = next((d for d in candidates if (d / CONFIG).is_file()), None)
        return root and cls.load(root)

    @classmethod
    def load(cls, root: Path) -> Checkout:
        return cls(root, json.loads((root / CONFIG).read_text()))

    @property
    def king(self) -> str:
        return (self.root / 'KING').read_text().strip()

    def solver_dirs(self, login: str) -> list[Path]:
        """``solvers/<login>/<n>/``, by increasing n."""
        mine = self.root / 'solvers' / login
        numbered = [d for d in mine.iterdir() if d.is_dir() and d.name.isdigit()] if mine.is_dir() else []
        return sorted(numbered, key=lambda d: int(d.name))

    def next_solver_dir(self, login: str) -> Path:
        dirs = self.solver_dirs(login)
        return self.root / 'solvers' / login / str(int(dirs[-1].name) + 1 if dirs else 1)

    def summary(self) -> str:
        """The README's first paragraph that is not a heading."""
        readme = self.root / 'README.md'
        paragraphs = readme.read_text().split('\n\n') if readme.is_file() else []
        return next((p.strip() for p in paragraphs if p.strip() and not p.lstrip().startswith('#')), '')


def submission_error(att: Attestation, config: dict, challenger_sha: str, king_sha: str, block: int) -> str | None:
    """Why the maintainer would close this PR, or ``None``: every check it runs that a miner can run first."""
    signer, result = att.signer, att.result
    accepted = config.get('dev_attestation_pubkey')
    if signer.get('kind') == DEV and signer.get('pubkey') != accepted:
        return f"dev signer {signer.get('pubkey')} is not the challenge's dev_attestation_pubkey ({accepted})"
    try:
        if not verify(att, accepted):
            return 'the attestation signature does not verify'
    except NotImplementedError as e:
        return str(e)
    if not result.get('crown'):
        return 'the attested result is not a crown'
    expected = {'module': config['module'], 'tier': config['tier'], 'n': config['seeds'], 'margin': config['margin']}
    if mismatch := [f'{k} {result.get(k)!r} != {v!r}' for k, v in expected.items() if result.get(k) != v]:
        return f'the run does not match challenge.json: {", ".join(mismatch)}'
    if result['challenger']['sha'] != challenger_sha:
        return f'the attested solver {result["challenger"]["sha"]} is not this one ({challenger_sha}): re-run attest'
    if result['king']['sha'] != king_sha:
        return f'stale: attested against KING {result["king"]["sha"]}, KING is now {king_sha}: re-run attest'
    age = block - att.seed_block
    if not 0 <= age <= config['freshness_blocks']:
        return f'stale: seed block {att.seed_block} is {age} blocks old (limit {config["freshness_blocks"]})'
    return None
