# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for deterministic stake-weighted aggregation of repo weight preferences."""

import json
import random

from gittensor.validator.utils.config import (
    CONSENSUS_MIN_VALIDATOR_STAKE_RAO,
    CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS,
    CONSENSUS_WEIGHT_PRECISION,
)
from gittensor.validator.weight_consensus.codec import encode_prefs
from gittensor.validator.weight_consensus.consensus import aggregate_preferences, compute_snapshot_block

STAKE_30K = CONSENSUS_MIN_VALIDATOR_STAKE_RAO


def _aggregate(voters):
    """voters: list of (hotkey, stake, permit, prefs-or-raw-bytes-or-None)."""
    commitments, stakes, permits = {}, {}, {}
    for hotkey, stake, permit, prefs in voters:
        stakes[hotkey] = stake
        permits[hotkey] = permit
        if isinstance(prefs, bytes):
            commitments[hotkey] = prefs
        elif prefs is not None:
            commitments[hotkey] = encode_prefs(prefs)
    return aggregate_preferences(commitments, stakes, permits)


class TestSnapshotBlock:
    def test_boundaries(self):
        interval = CONSENSUS_SNAPSHOT_INTERVAL_BLOCKS
        assert compute_snapshot_block(0) == 0
        assert compute_snapshot_block(interval - 1) == 0
        assert compute_snapshot_block(interval) == interval
        assert compute_snapshot_block(2 * interval - 1) == interval


class TestAggregation:
    def test_stake_weighted_mean_golden_vector(self):
        result = _aggregate(
            [
                ('hk1', 3 * STAKE_30K, True, {'a/b': 65535}),
                ('hk2', STAKE_30K, True, {'a/b': 32768, 'c/d': 32767}),
            ]
        )
        s1, s2, prec = 3 * STAKE_30K, STAKE_30K, CONSENSUS_WEIGHT_PRECISION
        expected_ab = s1 * 65535 * prec // 65535 + s2 * 32768 * prec // 65535
        expected_cd = s2 * 32767 * prec // 65535
        assert result.numerators == {'a/b': expected_ab, 'c/d': expected_cd}
        total = expected_ab + expected_cd
        assert result.shares == {'a/b': expected_ab / total, 'c/d': expected_cd / total}
        assert result.voter_count == 2

    def test_filters_no_permit_and_low_stake(self):
        result = _aggregate(
            [
                ('miner', 100 * STAKE_30K, False, {'m/spam': 65535}),
                ('small', STAKE_30K - 1, True, {'s/small': 65535}),
                ('vali', STAKE_30K, True, {'a/b': 65535}),
            ]
        )
        assert set(result.shares) == {'a/b'}
        assert result.eligible_stake_rao == STAKE_30K

    def test_invalid_payload_counts_toward_eligible_not_valid(self):
        result = _aggregate(
            [
                ('bad', STAKE_30K, True, b'\x00junk'),
                ('good', STAKE_30K, True, {'a/b': 65535}),
            ]
        )
        assert result.eligible_stake_rao == 2 * STAKE_30K
        assert result.valid_stake_rao == STAKE_30K
        assert result.shares == {'a/b': 1.0}

    def test_activation_gate_below_half_returns_none_shares(self):
        result = _aggregate(
            [
                ('silent', 3 * STAKE_30K, True, None),
                ('voter', STAKE_30K, True, {'a/b': 65535}),
            ]
        )
        assert result.shares is None
        assert result.numerators  # still computed for the cache

    def test_activation_gate_exact_half_passes(self):
        result = _aggregate(
            [
                ('silent', STAKE_30K, True, None),
                ('voter', STAKE_30K, True, {'a/b': 65535}),
            ]
        )
        assert result.shares == {'a/b': 1.0}

    def test_zero_eligible_stake_gate_fails(self):
        result = _aggregate([('miner', STAKE_30K, False, {'a/b': 65535})])
        assert result.shares is None
        assert result.eligible_stake_rao == 0

    def test_per_voter_normalization(self):
        # Same stake, same single repo, wildly different basket totals — equal influence.
        result = _aggregate(
            [
                ('hk1', STAKE_30K, True, {'a/b': 10}),
                ('hk2', STAKE_30K, True, {'c/d': 65535}),
            ]
        )
        assert abs(result.shares['a/b'] - result.shares['c/d']) < 1e-12

    def test_shares_sum_to_one(self):
        random.seed(3)
        voters = [
            (
                f'hk{i}',
                STAKE_30K * random.randint(1, 20),
                True,
                {f'o/r{j}': random.randint(1, 65535) for j in range(random.randint(1, 10))},
            )
            for i in range(12)
        ]
        result = _aggregate(voters)
        assert abs(sum(result.shares.values()) - 1.0) < 1e-9

    def test_determinism_under_shuffled_input_order(self):
        random.seed(11)
        voters = [
            (
                f'hk{i:02d}',
                STAKE_30K * random.randint(1, 50),
                random.random() > 0.2,
                {f'own{j}/repo{j}': random.randint(1, 65535) for j in range(random.randint(1, 10))},
            )
            for i in range(20)
        ]
        baseline = None
        for _ in range(5):
            random.shuffle(voters)
            result = _aggregate(voters)
            serialized = json.dumps({'shares': result.shares, 'numerators': result.numerators}, sort_keys=True)
            assert baseline is None or serialized == baseline
            baseline = serialized
