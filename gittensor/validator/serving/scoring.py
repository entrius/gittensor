# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving speed credit and token pay (sub-subnet B beta).

Correctness is decided by the rolling ``AuditWindow`` (gittensor/serving/audit.py);
this module prices speed and tokens, both on served traffic. Speed: two
validator-observed numbers per served request — time to first streamed token
(network + queue + prefill), 1.0 up to SERVING_LATENCY_FULL_CREDIT_MS, 0.0 at
SERVING_LATENCY_ZERO_CREDIT_MS — and decode rate, completion tokens over
(total − TTFT), against what one honest card does on this runtime at the load this
validator itself had in flight to the miner (the release's blessing-time curve).
Credit = ttft_credit × decode_credit; decode under SERVING_DECODE_FLOOR_RATIO ×
expected is 0. The mean credit over a round's served requests is the miner's
routing weight. Pay: the output tokens the gateway saw a hotkey serve in the
round, in card-equivalents — ``tokens / (one card's aggregate decode tok/s ×
round seconds)`` — so an hour flat out on one card is one card-hour
(SERVING_GPU_HOUR_USD) and the per-token rate falls out of the release's speed.
"""

from typing import Dict, Optional, Sequence, Tuple

from gittensor.classes import RequestSpeed
from gittensor.constants import (
    SERVING_AGGREGATE_DECODE_TPS_FALLBACK,
    SERVING_DECODE_FLOOR_RATIO,
    SERVING_DECODE_MIN_TOKENS,
    SERVING_DECODE_PER_REQUEST_FALLBACK,
    SERVING_DECODE_TOLERANCE_RATIO,
    SERVING_GPU_HOUR_USD,
    SERVING_LATENCY_FULL_CREDIT_MS,
    SERVING_LATENCY_ZERO_CREDIT_MS,
)
from gittensor.serving.loadout import ServingRelease
from gittensor.serving.state import ServedRequest


def aggregate_decode_tps(release: ServingRelease) -> float:
    """Output tok/s one honest card sustains under load on ``release``: the token rate's physical base."""
    return release.aggregate_decode_tps or SERVING_AGGREGATE_DECODE_TPS_FALLBACK


def token_rate_usd(release: ServingRelease) -> float:
    """USD per output token: the card-hour target spread over what one card decodes in an hour."""
    return SERVING_GPU_HOUR_USD / (aggregate_decode_tps(release) * 3600.0)


def card_equivalents(tokens: int, release: ServingRelease, seconds: float) -> float:
    """Cards it takes to decode ``tokens`` output tokens in ``seconds`` on ``release``: 1.0 is one card flat out."""
    if tokens <= 0 or seconds <= 0:
        return 0.0
    return tokens / (aggregate_decode_tps(release) * seconds)


def paid_tokens(req: ServedRequest) -> int:
    """Output tokens the gateway saw ``req`` serve, never more than it asked for."""
    n = len(req.tokens or [])
    return min(n, req.max_tokens) if req.max_tokens > 0 else n


def latency_credit(elapsed_ms: float, full_ms: Optional[float] = None, zero_ms: Optional[float] = None) -> float:
    """``full_ms`` / ``zero_ms`` are the release's blessed bands; None falls back to the constants."""
    full = full_ms if full_ms is not None else SERVING_LATENCY_FULL_CREDIT_MS
    zero = zero_ms if zero_ms is not None else SERVING_LATENCY_ZERO_CREDIT_MS
    if elapsed_ms <= full:
        return 1.0
    if elapsed_ms >= zero:
        return 0.0
    return 1.0 - (elapsed_ms - full) / (zero - full)


def expected_decode_tps(curve: Optional[Dict[int, float]], inflight: int) -> float:
    """Per-request decode tok/s one honest card delivers at ``inflight`` concurrent requests (piecewise-linear)."""
    points: Sequence[Tuple[int, float]] = sorted(curve.items()) if curve else SERVING_DECODE_PER_REQUEST_FALLBACK
    n = max(1, int(inflight))
    if n <= points[0][0]:
        return points[0][1]
    for (n0, t0), (n1, t1) in zip(points, points[1:]):
        if n <= n1:
            return t0 + (t1 - t0) * (n - n0) / (n1 - n0)
    return points[-1][1]


def decode_credit(
    observed_tps: float,
    expected_tps: float,
    floor_ratio: float = SERVING_DECODE_FLOOR_RATIO,
    tolerance_ratio: float = SERVING_DECODE_TOLERANCE_RATIO,
) -> float:
    """1.0 down to ``tolerance_ratio`` x expected, then linear, 0 under ``floor_ratio`` x expected."""
    if expected_tps <= 0:
        return 1.0
    ratio = observed_tps / expected_tps
    return 0.0 if ratio < floor_ratio else min(1.0, ratio / tolerance_ratio)


def request_speed(req: ServedRequest, release: ServingRelease) -> RequestSpeed:
    """Speed of one verified served request: TTFT band × decode band (decode only when measurable)."""
    speed_ms = req.ttft_ms if req.ttft_ms is not None else req.latency_ms
    if speed_ms is None:
        return RequestSpeed(credit=0.0)
    credit = latency_credit(speed_ms, release.ttft_full_ms, release.ttft_zero_ms)
    tokens = len(req.tokens or [])
    decode_tps = None
    if (
        tokens >= SERVING_DECODE_MIN_TOKENS
        and req.ttft_ms is not None
        and req.latency_ms is not None
        and req.latency_ms > req.ttft_ms
    ):
        decode_tps = tokens / ((req.latency_ms - req.ttft_ms) / 1000.0)
        credit *= decode_credit(decode_tps, expected_decode_tps(release.decode_per_request, req.inflight))
    return RequestSpeed(credit=credit, ttft_ms=req.ttft_ms, decode_tps=decode_tps)
