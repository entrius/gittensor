# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge submit``'s checks: a fresh crown against today's KING passes; anything the maintainer would close
on is refused with the reason."""

import dataclasses

import pytest

from gittensor.challenges.attestation import sign_dev
from gittensor.challenges.checkout import submission_error
from tests.challenges.conftest import CHALLENGE_JSON

CHALLENGER, KING = 'c' * 40, 'k' * 40
RESULT = {
    **{'challenge_id': 'fake-echo', 'module': CHALLENGE_JSON['module'], 'tier': 'small', 'n': 3, 'margin': 0.01},
    **{
        'seed_block_hash': 'ab' * 32,
        'challenger': {'sha': CHALLENGER},
        'king': {'sha': KING},
        'mean_gain': 0.05,
        'crown': True,
    },
}


def refusal(*args) -> str:
    return submission_error(*args) or 'passed'


@pytest.fixture
def signed(tmp_path):
    att = sign_dev(tmp_path / 'dev.key', RESULT, 1000)
    return att, {**CHALLENGE_JSON, 'dev_attestation_pubkey': att.signer['pubkey']}


def test_a_fresh_crown_against_todays_king_passes(signed):
    att, config = signed

    assert submission_error(att, config, CHALLENGER, KING, 1145, 'ab' * 32) is None


@pytest.mark.parametrize(
    'challenger, king, block, reason',
    [
        (CHALLENGER, KING, 1146, 'stale: seed block 1000 is 146 blocks old (limit 145'),
        (CHALLENGER, KING, 999, 'stale: seed block 1000 is -1 blocks old'),
        (CHALLENGER, 'n' * 40, 1001, 'stale: attested against KING'),
        ('x' * 40, KING, 1001, 'the attested solver'),
    ],
)
def test_a_stale_moved_king_or_other_solver_is_refused(signed, challenger, king, block, reason):
    att, config = signed

    assert reason in refusal(att, config, challenger, king, block, 'ab' * 32)


def test_a_seed_hash_that_is_not_the_chains_is_refused(signed):
    att, config = signed

    assert "not block 1000's on chain" in refusal(att, config, CHALLENGER, KING, 1001, 'cd' * 32)


def test_an_unaccepted_signer_a_tampered_result_a_loser_or_another_run_is_refused(signed, tmp_path):
    att, config = signed
    tampered = dataclasses.replace(att, result={**RESULT, 'mean_gain': 0.5})
    loser = sign_dev(tmp_path / 'dev.key', {**RESULT, 'crown': False}, 1000)
    fewer = sign_dev(tmp_path / 'dev.key', {**RESULT, 'n': 2}, 1000)
    other = sign_dev(tmp_path / 'dev.key', {**RESULT, 'challenge_id': 'other'}, 1000)

    assert 'not the challenge' in refusal(att, CHALLENGE_JSON, CHALLENGER, KING, 1001, 'ab' * 32)
    assert 'does not verify' in refusal(tampered, config, CHALLENGER, KING, 1001, 'ab' * 32)
    assert 'not a crown' in refusal(loser, config, CHALLENGER, KING, 1001, 'ab' * 32)
    assert 'n 2 != 3' in refusal(fewer, config, CHALLENGER, KING, 1001, 'ab' * 32)
    assert "challenge_id 'other'" in refusal(other, config, CHALLENGER, KING, 1001, 'ab' * 32)
