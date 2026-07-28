"""Exact integer port of the repos-v0 price curve (smart-contracts/repos-v0/price.rs).

Bittensor-style registration price decay: whole halvings by shift plus a Q32
fractional multiply. Must reproduce the contract's golden vectors byte-for-byte,
so every operation mirrors the saturating u64/u128 Rust arithmetic — no floats.
"""

from functools import lru_cache

U64_MAX = (1 << 64) - 1

ONE_Q32 = 1 << 32
HALF_Q32 = 1 << 31


def mul_by_q32(value: int, factor_q32: int) -> int:
    """Multiply an integer by a Q32 fixed-point factor, saturating into u64."""
    return min((value * factor_q32) >> 32, U64_MAX)


def pow_q32(base_q32: int, exp: int) -> int:
    """Exponentiation-by-squaring for Q32 values: `base_q32 ^ exp` in Q32."""
    result = ONE_Q32
    factor = base_q32
    power = exp
    while power > 0:
        if power & 1:
            result = mul_by_q32(result, factor)
        power >>= 1
        if power > 0:
            factor = mul_by_q32(factor, factor)
    return result


@lru_cache(maxsize=None)
def decay_factor_q32(half_life: int) -> int:
    """Per-block decay factor `f` in Q32 such that `f ^ half_life = 0.5` (bisection)."""
    if half_life == 0:
        return ONE_Q32
    lo = 0
    hi = ONE_Q32
    while lo + 1 < hi:
        mid = lo + ((hi - lo) >> 1)
        if pow_q32(mid, half_life) > HALF_Q32:
            hi = mid
        else:
            lo = mid
    return lo


def lazy_price(last: int, delta_blocks: int, half_life: int, floor: int, ceiling: int) -> int:
    """Lazy price quote: `clamp(last * 0.5^(delta/half_life), floor, ceiling)`."""
    price = last
    if half_life > 0 and delta_blocks > 0:
        whole = delta_blocks // half_life
        frac = delta_blocks % half_life
        price = price >> whole if whole < 64 else 0
        if frac > 0:
            price = mul_by_q32(price, pow_q32(decay_factor_q32(half_life), frac))
    return max(floor, min(price, ceiling))
