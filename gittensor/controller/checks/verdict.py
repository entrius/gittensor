# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What a full check produces: one ``CheckResult`` per sub-check and a ``CheckVerdict`` that names the failures."""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ADMIT = 'ADMIT'
BENCH = 'BENCH'


@dataclass
class CheckResult:
    """``skipped`` marks a check that did not run because an earlier one failed (the GPU proof fills ~30 GB of VRAM
    per card, so it is not run on a box that already failed identity). A skipped check is not a named failure, but a
    verdict with any skipped check is never ADMIT."""

    name: str
    passed: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    def as_dict(self) -> dict:
        out = {'name': self.name, 'pass': self.passed, 'evidence': self.evidence}
        if self.skipped:
            out['skipped'] = True
        return out


@dataclass
class CheckVerdict:
    verdict: str  # ADMIT | BENCH
    checks: List[CheckResult]
    gpu_uuids: List[str] = field(default_factory=list)
    card_name: str = ''
    driver: str = ''
    checked_at: float = field(default_factory=time.time)

    @property
    def admitted(self) -> bool:
        return self.verdict == ADMIT

    @property
    def failed(self) -> List[str]:
        return [c.name for c in self.checks if not c.passed and not c.skipped]

    @property
    def skipped(self) -> List[str]:
        return [c.name for c in self.checks if c.skipped]

    def check(self, name: str) -> Optional[CheckResult]:
        return next((c for c in self.checks if c.name == name), None)

    def as_dict(self) -> dict:
        return {
            'verdict': self.verdict,
            'failed': self.failed,
            'skipped': self.skipped,
            'gpu_uuids': self.gpu_uuids,
            'card_name': self.card_name,
            'driver': self.driver,
            'checked_at': self.checked_at,
            'checks': [c.as_dict() for c in self.checks],
        }

    @classmethod
    def from_checks(
        cls, checks: List[CheckResult], gpu_uuids: List[str], card_name: str = '', driver: str = '', now=None
    ) -> 'CheckVerdict':
        clean = all(c.passed and not c.skipped for c in checks)
        return cls(
            ADMIT if clean and checks else BENCH,
            checks,
            list(gpu_uuids),
            card_name,
            driver,
            now if now is not None else time.time(),
        )
