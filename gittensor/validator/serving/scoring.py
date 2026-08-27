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

from gittensor.constants import (
    SERVING_LATENCY_FULL_CREDIT_MS,
    SERVING_LATENCY_ZERO_CREDIT_MS,
)


def latency_credit(elapsed_ms: float) -> float:
    if elapsed_ms <= SERVING_LATENCY_FULL_CREDIT_MS:
        return 1.0
    if elapsed_ms >= SERVING_LATENCY_ZERO_CREDIT_MS:
        return 0.0
    span = SERVING_LATENCY_ZERO_CREDIT_MS - SERVING_LATENCY_FULL_CREDIT_MS
    return 1.0 - (elapsed_ms - SERVING_LATENCY_FULL_CREDIT_MS) / span
