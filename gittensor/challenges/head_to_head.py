# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A challenger head to head against the king (the current crown) on the same seeds, and whether it takes the crown.

On the paired per-seed scores c and k (an invalid seed or a timeout scores 0): ``mean_gain = mean(c - k) / mean(k)``,
and ``lower_99`` is the 1st percentile of that ratio over ``RESAMPLES`` bootstrap resamples of the seeds, drawn by
numpy's default generator seeded with the seed block hash. The challenger takes the crown when every one of its seeds
is valid and ``lower_99 >= margin``. A king that scores 0 on every seed leaves no ratio: ``mean_gain`` and ``lower_99``
are null, and a fully valid challenger with a positive mean takes the crown.

The report is canonical JSON (sorted keys, compact separators, no NaN), so the same inputs give the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import numpy as np

from gittensor.challenges.runner import SeedResult, solver_cpus

RESAMPLES = 10_000


@dataclass(frozen=True)
class Entry:
    sha: str
    results: list[SeedResult]

    @property
    def scores(self) -> list[float]:
        return [r.score for r in self.results]

    def summary(self) -> dict:
        valid = sum(r.valid for r in self.results)
        return {'sha': self.sha, 'scores': self.scores, 'valid': valid, 'mean': sum(self.scores) / len(self.scores)}


def solver_sha(solver_dir: Path) -> str:
    """sha256 of the tree: per file, in relative-path order, its path, executable bit and bytes (a link: its target)."""
    entries = {}
    for root, dirs, files in os.walk(solver_dir):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                entries[os.path.relpath(path, solver_dir)] = ('l', os.readlink(path).encode())
            elif stat.S_ISREG(st.st_mode):
                data = Path(path).read_bytes()
                entries[os.path.relpath(path, solver_dir)] = ('x' if st.st_mode & 0o111 else '-', data)
    digest = hashlib.sha256()
    for rel, (kind, data) in sorted(entries.items()):
        digest.update(f'{rel}\0{kind}\0{len(data)}\0'.encode() + data)
    return digest.hexdigest()


def verdict(challenger: Entry, king: Entry, seed_block_hash: str, margin: float) -> dict:
    c, k = np.array(challenger.scores), np.array(king.scores)
    all_valid = all(r.valid for r in challenger.results)
    if not k.any():
        return {'mean_gain': None, 'lower_99': None, 'crown': all_valid and bool(c.any())}
    picks = np.random.default_rng(int(seed_block_hash, 16)).integers(0, len(c), (RESAMPLES, len(c)))
    with np.errstate(divide='ignore', invalid='ignore'):
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
        'challenger': challenger.summary(),
        'king': king.summary(),
        **verdict(challenger, king, seed_block_hash, margin),
    }


def canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False)
