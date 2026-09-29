# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``challenges.json``: which challenges run, from which package and version, at which tier, and each one's share of the
challenge pool (fractions; the slack recycles). A challenge package is imported by module name at run time, so
gittensor never depends on one; its ``CHALLENGE_ID`` and ``VERSION`` must match the entry.

Every ``emission_share`` must stay 0 until the attested evaluator image (Polaris) runs the runner: the local sandbox is
defense in depth, not proof of what ran."""

from __future__ import annotations

import importlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REGISTRY_PATH = Path(__file__).resolve().parent.parent / 'validator' / 'weights' / 'challenges.json'


class RegistryError(ValueError):
    """The registry or a challenge package cannot be used."""


@dataclass(frozen=True)
class Challenge:
    id: str
    module: str
    repo: str
    version: str
    tier: str
    seeds: int
    dethrone_margin: float
    emission_share: float


def load_registry(path: str | Path = REGISTRY_PATH) -> dict[str, Challenge]:
    try:
        raw = json.loads(Path(path).read_text())
        challenges = {
            cid: Challenge(
                id=cid,
                module=str(e['module']),
                repo=str(e['repo']),
                version=str(e['version']),
                tier=str(e['tier']),
                seeds=int(e['seeds']),
                dethrone_margin=float(e['dethrone_margin']),
                emission_share=float(e['emission_share']),
            )
            for cid, e in raw.items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise RegistryError(f'{path}: {e!r}') from e
    for c in challenges.values():
        if ':' in c.id or c.seeds < 1 or not 0 <= c.dethrone_margin < math.inf or not 0 <= c.emission_share <= 1:
            raise RegistryError(f'{path}: {c.id}: bad entry {c}')
    total = math.fsum(c.emission_share for c in challenges.values())
    if total > 1:
        raise RegistryError(f'{path}: emission shares sum to {total:.6f} > 1')
    return challenges


def import_challenge(challenge: Challenge) -> ModuleType:
    try:
        module = importlib.import_module(challenge.module)
    except ImportError as e:
        raise RegistryError(f'{challenge.id}: cannot import {challenge.module} ({e}); install {challenge.repo}') from e
    found = (getattr(module, 'CHALLENGE_ID', None), getattr(module, 'VERSION', None))
    if found != (challenge.id, challenge.version):
        raise RegistryError(f'{challenge.module} is {found}, the registry wants {(challenge.id, challenge.version)}')
    if challenge.tier not in module.TIERS:
        raise RegistryError(f'{challenge.id}: no tier {challenge.tier!r}')
    return module
