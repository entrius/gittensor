# Entrius 2025

"""Golden-vector tests for the repos-v0 price replica (must match tests.rs exactly)."""

import pytest

from gittensor.validator.repo_registry.price import (
    HALF_Q32,
    ONE_Q32,
    U64_MAX,
    decay_factor_q32,
    lazy_price,
    mul_by_q32,
    pow_q32,
)

GOLDEN_HALF_LIFE = 100_800
GOLDEN_FLOOR = 500_000_000_000
GOLDEN_CEILING = 500_000_000_000_000

# (price_last, delta_blocks, expected_quote) — mirrors PRICE_GOLDEN in
# smart-contracts/repos-v0/tests.rs byte-for-byte.
PRICE_GOLDEN = [
    (500_000_000_000, 0, 500_000_000_000),
    (500_000_000_000, 100_800, 500_000_000_000),
    (1_000_000_000_000, 100_800, 500_000_000_000),
    (500_000_000_000_000, 1, 499_996_561_789_885),
    (500_000_000_000_000, 50_400, 353_551_803_971_640),
    (500_000_000_000_000, 100_800, 250_000_000_000_000),
    (500_000_000_000_000, 201_600, 125_000_000_000_000),
    (500_000_000_000_000, 252_000, 88_387_950_992_910),
    (500_000_000_000_000, 6_451_200, 500_000_000_000),
    (123_456_789_012_345, 12_345, 113_408_938_323_092),
    (600_000_000_000_000, 0, 500_000_000_000_000),
    (400_000_000_000, 0, 500_000_000_000),
    (2_000_000_000_000, 33_600, 1_587_396_302_726),
    (2_000_000_000_000, 302_400, 500_000_000_000),
]

# decay_factor_q32(100_800) — golden constant shared with the contract.
DECAY_FACTOR_GOLDEN = 4_294_937_762


@pytest.mark.parametrize('last, delta, expected', PRICE_GOLDEN)
def test_price_golden_vectors(last, delta, expected):
    assert lazy_price(last, delta, GOLDEN_HALF_LIFE, GOLDEN_FLOOR, GOLDEN_CEILING) == expected


def test_decay_factor_golden():
    assert decay_factor_q32(GOLDEN_HALF_LIFE) == DECAY_FACTOR_GOLDEN


def test_mul_by_q32_basics():
    assert mul_by_q32(1_000, ONE_Q32) == 1_000
    assert mul_by_q32(1_000, HALF_Q32) == 500
    assert mul_by_q32(U64_MAX, U64_MAX) == U64_MAX
    assert mul_by_q32(0, U64_MAX) == 0


def test_pow_q32_basics():
    assert pow_q32(HALF_Q32, 0) == ONE_Q32
    assert pow_q32(HALF_Q32, 1) == HALF_Q32
    assert pow_q32(HALF_Q32, 3) == 1 << 29


def test_decay_factor_edge_cases():
    assert decay_factor_q32(0) == ONE_Q32
    assert decay_factor_q32(1) == HALF_Q32


def test_lazy_price_zero_half_life_only_clamps():
    assert lazy_price(700, 1_000_000, 0, 100, 500) == 500
    assert lazy_price(50, 1_000_000, 0, 100, 500) == 100


def test_lazy_price_huge_delta_hits_floor():
    delta = GOLDEN_HALF_LIFE * 64
    assert lazy_price(GOLDEN_CEILING, delta, GOLDEN_HALF_LIFE, GOLDEN_FLOOR, GOLDEN_CEILING) == GOLDEN_FLOOR


def test_price_bump_doubles():
    assert mul_by_q32(1_000, 2 << 32) == 2_000
