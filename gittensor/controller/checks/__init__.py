# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The full hardware check (``24`` §3 WS-C): scrape a box over SSH, judge it against the pinned spec, run the GPU
proof in the slot on every card at once, and say ADMIT or BENCH with the failing checks named.

The heavy names are resolved lazily: ``proof.slot`` imports ``checks.config`` and ``checks.scrape``, and importing
``full_check`` here eagerly would close that loop."""

import importlib
from typing import Any

from gittensor.controller.checks.runner import CommandResult, FakeRunner, HostRunner
from gittensor.controller.checks.verdict import ADMIT, BENCH, CheckResult, CheckVerdict

_LAZY = {
    'FullCheckConfig': 'gittensor.controller.checks.full_check',
    'run_full_check': 'gittensor.controller.checks.full_check',
    'NvmlAllowlist': 'gittensor.controller.checks.nvml_allowlist',
    'BoxState': 'gittensor.controller.checks.state',
    'StateStore': 'gittensor.controller.checks.state',
    'apply_verdict': 'gittensor.controller.checks.state',
    'backoff_seconds': 'gittensor.controller.checks.state',
    'release_from_bench': 'gittensor.controller.checks.state',
}

__all__ = [
    'ADMIT',
    'BENCH',
    'CheckResult',
    'CheckVerdict',
    'CommandResult',
    'FakeRunner',
    'HostRunner',
    *sorted(_LAZY),
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    return getattr(importlib.import_module(module), name)
