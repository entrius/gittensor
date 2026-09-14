# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The per-GPU-type rate table, ``fleet_pay.json`` (vault ``23`` §7a).

One row per GPU type: the idle and leased targets in USD per card-hour and the fleet size the pool is meant to pay
at those targets. USD is the target and alpha is what is paid — the ledger converts at the oracle price each
window — so editing this file is the whole pricing knob. The one invariant is leased > idle: otherwise nobody
wants to be used and the pool is Lium's idle subsidy with extra steps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RATES_PATH = Path(__file__).with_name('fleet_pay.json')


class RatesError(ValueError):
    """The table is malformed or breaks the leased > idle invariant. Nothing should be paid from it."""


@dataclass(frozen=True)
class GpuRate:
    gpu_type: str
    idle_usd_per_hr: float
    leased_usd_per_hr: float
    target_fleet: int

    @property
    def leased_to_idle(self) -> float:
        """What a leased second is worth in idle seconds, by construction of the single weighted pool."""
        return self.leased_usd_per_hr / self.idle_usd_per_hr if self.idle_usd_per_hr else float('inf')


def load_rates(path: str | Path = DEFAULT_RATES_PATH) -> dict[str, GpuRate]:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise RatesError(f'{path}: {e}') from e
    if not isinstance(doc, dict):
        raise RatesError(f'{path}: not a JSON object')
    rates: dict[str, GpuRate] = {}
    for gpu_type, row in doc.items():
        if gpu_type.startswith('_'):
            continue
        try:
            rate = GpuRate(
                gpu_type,
                float(row['idle_usd_per_hr']),
                float(row['leased_usd_per_hr']),
                int(row['target_fleet']),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise RatesError(f'{path}: {gpu_type}: {e!r}') from e
        if rate.idle_usd_per_hr < 0 or rate.leased_usd_per_hr <= 0 or rate.target_fleet <= 0:
            raise RatesError(f'{path}: {gpu_type}: rates must be non-negative, leased and target_fleet positive')
        if rate.leased_usd_per_hr <= rate.idle_usd_per_hr:
            raise RatesError(
                f'{path}: {gpu_type}: leased ({rate.leased_usd_per_hr}) must out-earn idle ({rate.idle_usd_per_hr})'
            )
        rates[gpu_type] = rate
    if not rates:
        raise RatesError(f'{path}: no GPU types')
    return rates
