# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What a full check produces: one ``CheckResult`` per sub-check and a ``CheckVerdict`` that names the failures."""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ADMIT = 'ADMIT'
BENCH = 'BENCH'
NOT_RUN = 'NOT_RUN'  # no check failed, but one could not be carried out: no answer was judged


@dataclass
class CheckResult:
    """``skipped`` marks a check that did not run because an earlier one failed (the GPU proof fills ~30 GB of VRAM
    per card, so it is not run on a box that already failed identity). A skipped check is not a named failure, but a
    verdict with any skipped check is never ADMIT. ``not_run`` marks a check that could not be carried out (the proof
    container would not start): the box gave no answer to judge, so it is neither a pass nor a named failure."""

    name: str
    passed: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    not_run: bool = False

    def as_dict(self) -> dict:
        out = {'name': self.name, 'pass': self.passed, 'evidence': self.evidence}
        if self.skipped:
            out['skipped'] = True
        if self.not_run:
            out['not_run'] = True
        return out


@dataclass
class CheckVerdict:
    verdict: str  # ADMIT | BENCH | NOT_RUN
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
        return [c.name for c in self.checks if not c.passed and not c.skipped and not c.not_run]

    @property
    def skipped(self) -> List[str]:
        return [c.name for c in self.checks if c.skipped]

    @property
    def not_run(self) -> List[str]:
        return [c.name for c in self.checks if c.not_run]

    def check(self, name: str) -> Optional[CheckResult]:
        return next((c for c in self.checks if c.name == name), None)

    def as_dict(self) -> dict:
        return {
            'verdict': self.verdict,
            'failed': self.failed,
            'skipped': self.skipped,
            'not_run': self.not_run,
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
        # A named failure is a BENCH whatever else could not run; NOT_RUN is only "nothing failed, something never ran".
        only_not_run = any(c.not_run for c in checks) and all(c.passed or c.not_run for c in checks)
        return cls(
            ADMIT if clean and checks else NOT_RUN if only_not_run else BENCH,
            checks,
            list(gpu_uuids),
            card_name,
            driver,
            now if now is not None else time.time(),
        )
