# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving challenge scoring (sub-subnet B beta).

score per audit = correctness (0/1 audit-bank tolerance verdict) x latency credit.
Latency credit is 1.0 up to SERVING_LATENCY_FULL_CREDIT_MS and falls linearly
to 0.0 at SERVING_LATENCY_ZERO_CREDIT_MS, so a correct-but-slow response earns
partially and a wrong response earns nothing regardless of speed. A miner's
round score is the mean over its challenges — misses count as zero, which
folds availability into the same number.
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


def challenge_score(correct: bool, elapsed_ms: float) -> float:
    if not correct:
        return 0.0
    return latency_credit(elapsed_ms)
