//! Bittensor-style registration price decay: whole halvings by shift plus a Q32
//! fractional multiply. Ports subtensor `mul_by_q32` / `pow_q32` / `decay_factor_q32`
//! (pallets/subtensor/src/utils/misc.rs) with u64 exponents for lazy evaluation.
//! All arithmetic is saturating/clamped — no panics.

/// 1.0 in Q32
pub const ONE_Q32: u64 = 1 << 32;
/// 0.5 in Q32
pub const HALF_Q32: u64 = 1 << 31;

/// Multiply an integer by a Q32 fixed-point factor, saturating into u64.
pub fn mul_by_q32(value: u64, factor_q32: u64) -> u64 {
    let product = (value as u128).saturating_mul(factor_q32 as u128);
    let shifted = product >> 32;
    core::cmp::min(shifted, u64::MAX as u128) as u64
}

/// Exponentiation-by-squaring for Q32 values: `base_q32 ^ exp` in Q32.
pub fn pow_q32(base_q32: u64, exp: u64) -> u64 {
    let mut result = ONE_Q32;
    let mut factor = base_q32;
    let mut power = exp;
    while power > 0 {
        if power & 1 == 1 {
            result = mul_by_q32(result, factor);
        }
        power >>= 1;
        if power > 0 {
            factor = mul_by_q32(factor, factor);
        }
    }
    result
}

/// Per-block decay factor `f` in Q32 such that `f ^ half_life = 0.5` (bisection).
pub fn decay_factor_q32(half_life: u64) -> u64 {
    if half_life == 0 {
        return ONE_Q32;
    }
    let mut lo = 0u64;
    let mut hi = ONE_Q32;
    while lo.saturating_add(1) < hi {
        let mid = lo.saturating_add(hi.saturating_sub(lo) >> 1);
        if pow_q32(mid, half_life) > HALF_Q32 {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    lo
}

/// Lazy price quote: `clamp(last * 0.5^(delta/half_life), floor, ceiling)`.
/// Whole halvings apply as a right shift; the fractional remainder applies as
/// `f^frac` where `f = decay_factor_q32(half_life)`.
pub fn lazy_price(last: u64, delta_blocks: u64, half_life: u64, floor: u64, ceiling: u64) -> u64 {
    let mut price = last;
    if half_life > 0 && delta_blocks > 0 {
        let whole = delta_blocks.checked_div(half_life).unwrap_or(0);
        let frac = delta_blocks.checked_rem(half_life).unwrap_or(0);
        price = u32::try_from(whole)
            .ok()
            .and_then(|shift| price.checked_shr(shift))
            .unwrap_or(0);
        if frac > 0 {
            price = mul_by_q32(price, pow_q32(decay_factor_q32(half_life), frac));
        }
    }
    core::cmp::max(floor, core::cmp::min(price, ceiling))
}
