# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving latency credit (sub-subnet B beta).

Correctness is decided by the rolling ``AuditWindow`` (gittensor/serving/audit.py);
this module only prices speed. The input is the validator-observed time to
first streamed token of a served request (network + queue + prefill): 1.0 up
to SERVING_LATENCY_FULL_CREDIT_MS, falling linearly to 0.0 at
SERVING_LATENCY_ZERO_CREDIT_MS. Total latency is not used — generation length
is the user's choice, and throughput is priced by the capacity probe. A miner's
round score is its window verdict (0/1) times the mean credit over the round's
served requests — misses earn 0 credit, which folds availability in.
"""

from typing import Optional

from gittensor.constants import (
    SERVING_LATENCY_FULL_CREDIT_MS,
    SERVING_LATENCY_ZERO_CREDIT_MS,
)


def latency_credit(elapsed_ms: float, full_ms: Optional[float] = None, zero_ms: Optional[float] = None) -> float:
    """``full_ms`` / ``zero_ms`` are the release's blessed bands; None falls back to the constants."""
    full = full_ms if full_ms is not None else SERVING_LATENCY_FULL_CREDIT_MS
    zero = zero_ms if zero_ms is not None else SERVING_LATENCY_ZERO_CREDIT_MS
    if elapsed_ms <= full:
        return 1.0
    if elapsed_ms >= zero:
        return 0.0
    return 1.0 - (elapsed_ms - full) / (zero - full)
