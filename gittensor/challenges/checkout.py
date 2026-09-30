# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A challenge repo's layout (G.1), defined once for the miner's commands and the maintainer's: ``.gittensor/
challenge.json``, ``KING``, ``solvers/<login>/<n>/`` and the PR's ``attestation.json`` and CLA line. Plus a miner's
clone of it, the checks ``gitt challenge submit`` runs before opening the PR, and the source-only rule every solver
meets (``source_error``), checked by eval, attest, submit and verify alike."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gittensor.challenges.attestation import DEV, Attestation, verify
from gittensor.challenges.head_to_head import SKIPPED
from gittensor.challenges.runner import BUILD, SOLVE

if TYPE_CHECKING:
    from gittensor.challenges.verify import Config

CONFIG = '.gittensor/challenge.json'
KING_FILE = 'KING'
ATTESTATION = 'attestation.json'
MAIN = 'main'
SOLVER_N = '[1-9][0-9]*'  # n in solvers/<login>/<n>/
CLA_TEXT = 'I agree to the Contributor License Agreement in CLA.md and that this solver is licensed under LICENSING.md.'
CLA_LINE = f'- [x] {CLA_TEXT}'
OWNER = 'entrius'
SLACK_BLOCKS = 5  # the PR opens a few blocks after submit checks freshness
SOURCE_FILES = 500
SOURCE_FILE_BYTES = 1 << 20
SOURCE_DIR_BYTES = 4 << 20
TEXT_MODES = ('100644', '100755')  # git's modes for a regular file; a symlink is 120000, a submodule 160000


def normalize_hash(value: str) -> str:
    """A block hash as attested and compared: lowercase hex without ``0x``."""
    return value.lower().removeprefix('0x')


def pr_body(solver: str, king: str, seed_block: int) -> str:
    return f'{solver} against KING {king} on seed block {seed_block}.\n\n{CLA_LINE}\n'


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
        return (self.root / KING_FILE).read_text().strip()

    def solver_dirs(self, login: str) -> list[Path]:
        """``solvers/<login>/<n>/``, by increasing n."""
        mine = self.root / 'solvers' / login
        numbered = [d for d in mine.iterdir() if d.is_dir() and re.fullmatch(SOLVER_N, d.name)] if mine.is_dir() else []
        return sorted(numbered, key=lambda d: int(d.name))

    def next_solver_dir(self, login: str, taken: list[str]) -> Path:
        """One past every n here and in ``taken`` (the names already on ``main``)."""
        used = [int(d.name) for d in self.solver_dirs(login)] + [int(n) for n in taken if re.fullmatch(SOLVER_N, n)]
        return self.root / 'solvers' / login / str(max(used, default=0) + 1)

    def summary(self) -> str:
        """The README's first paragraph that is not a heading."""
        readme = self.root / 'README.md'
        paragraphs = readme.read_text().split('\n\n') if readme.is_file() else []
        return next((p.strip() for p in paragraphs if p.strip() and not p.lstrip().startswith('#')), '')


@dataclass(frozen=True)
class SourceFile:
    """One file of a solver, from a directory, a git tree or the GitHub API alike."""

    path: str  # relative to the solver dir
    mode: str  # git's; anything not in TEXT_MODES is a link or special file
    size: int
    read: Callable[[], bytes]  # called only once the sizes pass


def source_error(files: Iterable[SourceFile]) -> str | None:
    """Why the solver is not source only, or ``None``. Source only: at most ``SOURCE_FILES`` regular files (no
    symlinks, submodules or special files), each valid UTF-8 without NUL bytes and at most ``SOURCE_FILE_BYTES``,
    ``SOURCE_DIR_BYTES`` in all; ``solve``, and ``build`` if there is one, scripts with a shebang at the root."""
    count = total = 0
    scripts = {}
    for f in files:
        count, total = count + 1, total + f.size
        if count > SOURCE_FILES:
            return f'more than {SOURCE_FILES} files'
        if f.mode not in TEXT_MODES:
            return f'{f.path} is a symlink or special file: source only'
        if f.size > SOURCE_FILE_BYTES:
            return f'{f.path} is {f.size} bytes, over the {SOURCE_FILE_BYTES} per file'
        if total > SOURCE_DIR_BYTES:
            return f'over {SOURCE_DIR_BYTES} bytes in all'
        if not is_text(data := f.read()):
            return f'{f.path} is binary: source only (UTF-8 text without NUL bytes)'
        if f.path in (SOLVE, BUILD):
            scripts[f.path] = data
    if SOLVE not in scripts:
        return f'no {SOLVE} script'
    unmarked = [name for name, data in scripts.items() if not data.startswith(b'#!')]
    return f'{unmarked[0]} is not a script: start it with a shebang (#!)' if unmarked else None


def is_text(data: bytes) -> bool:
    try:
        data.decode()
    except UnicodeDecodeError:
        return False
    return b'\0' not in data


def dir_files(root: Path, sub: str = '') -> Iterator[SourceFile]:
    """The files under ``root`` in name order, without ``SKIPPED`` names."""
    for entry in sorted(os.scandir(root / sub), key=lambda e: e.name):
        if entry.name in SKIPPED:
            continue
        path, st = f'{sub}{entry.name}', entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            yield from dir_files(root, f'{path}/')
        else:
            mode = ('100755' if st.st_mode & stat.S_IXUSR else '100644') if stat.S_ISREG(st.st_mode) else 'special'
            yield SourceFile(path, mode, st.st_size, (root / path).read_bytes)


def side(result: dict, name: str) -> dict:
    """``result[name]`` (the challenger or the king), or ``{}`` when it is not an object."""
    return value if isinstance(value := result.get(name), dict) else {}


def run_mismatch(result: dict, config: Config) -> str | None:
    """How the attested run's settings differ from ``challenge.json``, if they do."""
    wanted = {
        **{'challenge_id': config.challenge_id, 'module': config.module, 'tier': config.tier},
        **{'n': config.seeds, 'margin': config.margin},
    }
    ran = {key: result.get(key) for key in wanted}
    return None if ran == wanted else f'ran {ran}, challenge.json wants {wanted}'


def submission_error(
    att: Attestation, config: Config, challenger_sha: str, king_sha: str, block: int, seed_block_hash: str
) -> str | None:
    """Why the maintainer would close this PR, or ``None``: every check it runs that a miner can run first. ``block``
    is the chain's current block and ``seed_block_hash`` the chain's hash of the attested seed block."""
    signer, result = att.signer, att.result
    accepted = config.dev_attestation_pubkey
    if signer.get('kind') == DEV and signer.get('pubkey') != accepted:
        return f"dev signer {signer.get('pubkey')} is not the challenge's dev_attestation_pubkey ({accepted})"
    try:
        if not verify(att, accepted):
            return 'the attestation signature does not verify'
    except NotImplementedError as e:
        return str(e)
    if result.get('crown') is not True:
        return 'the attested result is not a crown'
    if mismatch := run_mismatch(result, config):
        return f'the run does not match challenge.json: {mismatch}'
    if (attested := side(result, 'challenger').get('sha')) != challenger_sha:
        return f'the attested solver {attested} is not this one ({challenger_sha}): re-run attest'
    if (attested := side(result, 'king').get('sha')) != king_sha:
        return f'stale: attested against KING {attested}, KING is now {king_sha}: re-run attest'
    if result.get('seed_block_hash') != seed_block_hash:
        return f"the attested seed block hash is not block {att.seed_block}'s on chain ({seed_block_hash})"
    age, limit = block - att.seed_block, config.freshness_blocks - SLACK_BLOCKS
    if not 0 <= age <= limit:
        return f'stale: seed block {att.seed_block} is {age} blocks old (limit {limit} here, so the PR is in time)'
    return None
