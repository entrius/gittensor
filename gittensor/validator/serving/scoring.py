# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving latency credit (sub-subnet B beta).

Correctness is decided by the rolling ``AuditWindow`` (gittensor/serving/audit.py);
this module only prices speed. Latency credit is 1.0 up to
SERVING_LATENCY_FULL_CREDIT_MS and falls linearly to 0.0 at
SERVING_LATENCY_ZERO_CREDIT_MS. A miner's round score is its window verdict
(0/1) times the mean latency credit over the round's audits — misses earn 0
credit, which folds availability into the same number.
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
