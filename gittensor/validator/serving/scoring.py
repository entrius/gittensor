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
routing weight. Pay: the card-time the gateway saw a hotkey serve in the round,
in card-equivalents — ``(output tokens / one card's aggregate decode tok/s +
prompt tokens / one card's prefill tok/s) / round seconds`` — so an hour flat
out on one card is one card-hour (SERVING_GPU_HOUR_USD) whether it spent it
decoding or prefilling, and both per-token rates fall out of the release's speed.
Prompt tokens are the miner-reported count, clamped to what the prompt could hold.
"""

from typing import Dict, Optional, Sequence, Tuple

from gittensor.classes import RequestSpeed
from gittensor.constants import (
    SERVING_AGGREGATE_DECODE_TPS_FALLBACK,
    SERVING_CHARS_PER_TOKEN_ESTIMATE,
    SERVING_DECODE_FLOOR_RATIO,
    SERVING_DECODE_MIN_TOKENS,
    SERVING_DECODE_PER_REQUEST_FALLBACK,
    SERVING_DECODE_TOLERANCE_RATIO,
    SERVING_GPU_HOUR_USD,
    SERVING_LATENCY_FULL_CREDIT_MS,
    SERVING_LATENCY_ZERO_CREDIT_MS,
    SERVING_PREFILL_TPS_FALLBACK,
    SERVING_PROMPT_TEMPLATE_TOKENS,
)
from gittensor.serving.loadout import ServingRelease
from gittensor.serving.state import ServedRequest


def aggregate_decode_tps(release: ServingRelease) -> float:
    """Output tok/s one honest card sustains under load on ``release``: the token rate's physical base."""
    return release.aggregate_decode_tps or SERVING_AGGREGATE_DECODE_TPS_FALLBACK


def token_rate_usd(release: ServingRelease) -> float:
    """USD per output token: the card-hour target spread over what one card decodes in an hour."""
    return SERVING_GPU_HOUR_USD / (aggregate_decode_tps(release) * 3600.0)


def prefill_tps(release: ServingRelease) -> float:
    """Prompt tok/s one honest card prefills on ``release``: the prompt-token rate's physical base."""
    return release.prefill_tps or SERVING_PREFILL_TPS_FALLBACK


def prompt_token_rate_usd(release: ServingRelease) -> float:
    """USD per prompt token: the card-hour target spread over what one card prefills in an hour."""
    return SERVING_GPU_HOUR_USD / (prefill_tps(release) * 3600.0)


def card_equivalents(tokens: int, release: ServingRelease, seconds: float, prompt_tokens: int = 0) -> float:
    """Cards it takes to decode ``tokens`` output tokens and prefill ``prompt_tokens`` in ``seconds`` on
    ``release``: 1.0 is one card flat out. Both are card-time on the same card, so the two per-token rates fall
    out of one card-hour and shifting work between prompt and completion never changes what it is worth."""
    if seconds <= 0:
        return 0.0
    decode_s = max(tokens, 0) / aggregate_decode_tps(release)
    prefill_s = max(prompt_tokens, 0) / prefill_tps(release)
    return (decode_s + prefill_s) / seconds


def paid_tokens(req: ServedRequest) -> int:
    """Output tokens the gateway saw ``req`` serve, never more than it asked for."""
    n = len(req.tokens or [])
    return min(n, req.max_tokens) if req.max_tokens > 0 else n


def prompt_token_ceiling(messages: Sequence[Dict[str, str]]) -> int:
    """The most prompt tokens ``messages`` can honestly tokenize to: one per character plus the chat template."""
    chars = sum(len(m.get('content') or '') + len(m.get('role') or '') for m in messages)
    return chars + SERVING_PROMPT_TEMPLATE_TOKENS * len(messages)


def paid_prompt_tokens(req: ServedRequest) -> int:
    """Prompt tokens ``req`` is paid prefill for: what the miner's runtime reported, never more than the prompt
    could hold. The count is miner-reported (the gateway does not tokenize), so the clamp bounds a lie to a few x
    of a rate that is already ~1/85 of an output token's."""
    return max(0, min(int(req.prompt_tokens or 0), prompt_token_ceiling(req.messages)))


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
    """Per-request decode tok/s one honest card delivers at ``inflight`` concurrent requests.

    The curve's points are per-request rates; between them the interpolation runs on the card's *aggregate* rate
    (per-request x concurrency), the quantity that is actually flat once a batch forms, and the per-request rate is
    that aggregate over ``inflight``. A straight line between per-request points instead sits far above the real
    hyperbola: with points at 1 and 6 only, an honest 5090 at 2-5 concurrent read 0.32-0.48x expected — under the
    floor, zero credit, exactly where every traffic burst spends its time (#1753). Past the last point the
    per-request rate holds: a runtime that queues beyond its batch keeps each stream's rate and grows TTFT.
    """
    points: Sequence[Tuple[int, float]] = sorted(curve.items()) if curve else SERVING_DECODE_PER_REQUEST_FALLBACK
    n = max(1, int(inflight))
    if n <= points[0][0]:
        return points[0][1]
    for (n0, t0), (n1, t1) in zip(points, points[1:]):
        if n <= n1:
            a0, a1 = n0 * t0, n1 * t1
            return (a0 + (a1 - a0) * (n - n0) / (n1 - n0)) / n
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


def prompt_token_estimate(messages: Sequence[Dict[str, str]]) -> int:
    """The validator's own count of a prompt's tokens when the reference reported none: ~4 characters each."""
    chars = sum(len(m.get('content') or '') + len(m.get('role') or '') for m in messages)
    return chars // SERVING_CHARS_PER_TOKEN_ESTIMATE


def prefill_allowance_ms(prompt_tokens: int, release: ServingRelease) -> float:
    """Time one honest card needs to prefill ``prompt_tokens`` on ``release``. Subtracted from the observed TTFT
    before the latency bands apply, so a 30k-token prompt that took 1.4 s to first token reads as a fast card, not a
    slow one; the bands then measure what they were set for — network, queue and the card's own overhead."""
    return 1000.0 * max(prompt_tokens, 0) / prefill_tps(release)


def request_speed(req: ServedRequest, release: ServingRelease, prompt_tokens: Optional[int] = None) -> RequestSpeed:
    """Speed of one verified served request: TTFT band × decode band (decode only when measurable).

    ``prompt_tokens`` is the reference's count of the prompt (``AuditVerdict.prompt_tokens``); None falls back to
    the validator's own estimate from the prompt text. The miner-reported ``req.prompt_tokens`` is deliberately not
    used here: a miner could inflate it to buy TTFT slack.
    """
    speed_ms = req.ttft_ms if req.ttft_ms is not None else req.latency_ms
    if speed_ms is None:
        return RequestSpeed(credit=0.0)
    n = prompt_tokens if prompt_tokens is not None else prompt_token_estimate(req.messages)
    residual_ms = max(0.0, speed_ms - prefill_allowance_ms(n, release))
    credit = latency_credit(residual_ms, release.ttft_full_ms, release.ttft_zero_ms)
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
