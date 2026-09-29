# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A challenge package in miniature (contract A): the instance is a number, the solver echoes it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHALLENGE_ID = 'fake-echo'
VERSION = '0.1.0'


@dataclass(frozen=True)
class Tier:
    time_limit_s: float
    memory_mb: int
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    valid: bool
    score: float
    reason: str = ''


TIERS = {'small': Tier(time_limit_s=1.0, memory_mb=256)}


def generate(seed: bytes, tier: str, instance_dir: Path, secret_dir: Path) -> None:
    number = str(int.from_bytes(seed[:8], 'big')).encode()
    (instance_dir / 'number.txt').write_bytes(number)
    (secret_dir / 'number.txt').write_bytes(number)


def check(instance_dir: Path, secret_dir: Path, output_dir: Path) -> Verdict:
    answer = output_dir / 'answer.txt'
    if not answer.is_file() or answer.read_bytes().strip() != (secret_dir / 'number.txt').read_bytes():
        return Verdict(False, 0.0, 'wrong answer')
    return Verdict(True, 1.0)
