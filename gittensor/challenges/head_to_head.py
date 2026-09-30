# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A challenger head to head against the king (the current crown) on the same seeds, and whether it takes the crown.

On the paired per-seed scores c and k (an invalid seed or a timeout scores 0): ``mean_gain = mean(c - k) / mean(k)``,
and ``lower_99`` is the 1st percentile of that ratio over ``RESAMPLES`` bootstrap resamples of the seeds: indices from
``np.random.default_rng(int(seed_block_hash, 16)).integers(0, n, (RESAMPLES, n))`` (PCG64), the percentile by
``np.quantile(ratios, 0.01, method='inverted_cdf')`` (always one resample's ratio, never interpolated). The challenger takes the crown when every one of its seeds
is valid and ``lower_99 >= margin``. A king that scores 0 on every seed leaves no ratio: ``mean_gain`` and ``lower_99``
are null, and a fully valid challenger with a positive mean takes the crown.

The report is canonical JSON (sorted keys, compact separators, no NaN or infinity: a score that overflows raises),
so the same inputs give the same bytes; it names the resamples and the gittensor and numpy versions that produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from types import ModuleType

import numpy as np

from gittensor.challenges.runner import SeedResult, solver_cpus

RESAMPLES = 10_000
SKIPPED = ('.git', '__pycache__')  # never hashed, never run


@dataclass(frozen=True)
class Entry:
    sha: str
    results: list[SeedResult]
    build: bool = False  # it has a build script

    @property
    def scores(self) -> list[float]:
        return [r.score for r in self.results]

    def summary(self) -> dict:
        valid = sum(r.valid for r in self.results)
        mean = sum(self.scores) / len(self.scores)
        return {'sha': self.sha, 'scores': self.scores, 'valid': valid, 'mean': mean, 'build': self.build}


def solver_sha(solver_dir: Path) -> str:
    """The git tree sha1 of the directory (``git rev-parse HEAD:<dir>``), without ``SKIPPED`` names or empty dirs."""
    return git_tree(str(solver_dir)).hex()


def snapshot(solver_dir: Path, dest: Path) -> Path:
    """A private copy of the solver, taken once: what is hashed and what every seed runs, without ``SKIPPED``."""
    return Path(shutil.copytree(solver_dir, dest, symlinks=True, ignore=shutil.ignore_patterns(*SKIPPED)))


def git_tree(path: str) -> bytes:
    entries = []
    for name in os.listdir(path):
        if name in SKIPPED:
            continue
        full, key = os.path.join(path, name), os.fsencode(name)
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            entries.append((key, b'120000', git_object(b'blob', os.fsencode(os.readlink(full)))))
        elif stat.S_ISREG(st.st_mode):
            mode = b'100755' if st.st_mode & stat.S_IXUSR else b'100644'
            entries.append((key, mode, git_object(b'blob', Path(full).read_bytes())))
        elif stat.S_ISDIR(st.st_mode) and (tree := git_tree(full)) != EMPTY_TREE:
            entries.append((key + b'/', b'40000', tree))  # git orders a tree as if its name ended in '/'
    body = b''.join(b'%s %s\0%s' % (mode, key.rstrip(b'/'), sha) for key, mode, sha in sorted(entries))
    return git_object(b'tree', body)


def git_object(kind: bytes, body: bytes) -> bytes:
    return hashlib.sha1(b'%s %d\0' % (kind, len(body)) + body).digest()


EMPTY_TREE = git_object(b'tree', b'')


def verdict(challenger: Entry, king: Entry, seed_block_hash: str, margin: float) -> dict:
    c, k = np.array(challenger.scores), np.array(king.scores)
    all_valid = all(r.valid for r in challenger.results)
    if not k.any():
        return {'mean_gain': None, 'lower_99': None, 'crown': all_valid and bool(c.any())}
    picks = np.random.default_rng(int(seed_block_hash, 16)).integers(0, len(c), (RESAMPLES, len(c)))
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        ratios = (c - k)[picks].mean(axis=1) / k[picks].mean(axis=1)
    # a resample of only k = 0 seeds: no gain when c is 0 too, else unbounded; under 1/e of them, so the bound is finite
    ratios = np.nan_to_num(ratios, nan=0.0, posinf=np.inf)
    lower = float(np.quantile(ratios, 0.01, method='inverted_cdf'))
    return {'mean_gain': float((c - k).mean() / k.mean()), 'lower_99': lower, 'crown': all_valid and lower >= margin}


def report(
    module: str, challenge: ModuleType, tier: str, seed_block_hash: str, margin: float, challenger: Entry, king: Entry
) -> dict:
    return {
        'module': module,
        'challenge_id': challenge.CHALLENGE_ID,
        'version': challenge.VERSION,
        'tier': tier,
        'n': len(challenger.results),
        'seed_block_hash': seed_block_hash,
        'margin': margin,
        'cpus': len(solver_cpus()),
        'resamples': RESAMPLES,
        'gittensor_version': version('gittensor'),
        'numpy_version': np.__version__,
        'challenger': challenger.summary(),
        'king': king.summary(),
        **verdict(challenger, king, seed_block_hash, margin),
    }


def canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False)
