# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge submit``'s checks: a fresh crown against today's KING passes; anything the maintainer would close
on is refused with the reason."""

import dataclasses
from datetime import datetime

import pytest

from gittensor.challenges.attestation import sign_dev
from gittensor.challenges.checkout import (
    ATTESTATION,
    SOURCE_FILE_BYTES,
    dir_files,
    pr_body,
    source_error,
    submission_error,
)
from gittensor.challenges.head_to_head import Entry, report
from gittensor.challenges.runner import SeedResult
from gittensor.challenges.verify import Chain, Config, PullRequest, Repo, decide
from tests.challenges import fake_challenge
from tests.challenges.conftest import CHALLENGE_JSON, FAKE_MODULE

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
    return att, Config(**{**CHALLENGE_JSON, 'dev_attestation_pubkey': att.signer['pubkey']})


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

    assert 'not the challenge' in refusal(att, Config(**CHALLENGE_JSON), CHALLENGER, KING, 1001, 'ab' * 32)
    assert 'does not verify' in refusal(tampered, config, CHALLENGER, KING, 1001, 'ab' * 32)
    assert 'not a crown' in refusal(loser, config, CHALLENGER, KING, 1001, 'ab' * 32)
    truthy = sign_dev(tmp_path / 'dev.key', {**RESULT, 'crown': 'yes'}, 1000)
    assert 'not a crown' in refusal(truthy, config, CHALLENGER, KING, 1001, 'ab' * 32)
    assert "'n': 2" in refusal(fewer, config, CHALLENGER, KING, 1001, 'ab' * 32)
    assert "'challenge_id': 'other'" in refusal(other, config, CHALLENGER, KING, 1001, 'ab' * 32)


def test_what_submit_accepts_the_maintainer_crowns(tmp_path):
    """The seam: an attested crown, submitted with submit's own PR body, passes both sides' checks."""
    challenger = Entry(CHALLENGER, [SeedResult(True, 1.1, 0.0)] * 3)
    king = Entry(KING, [SeedResult(True, 1.0, 0.0)] * 3)
    result = report(FAKE_MODULE, fake_challenge, 'small', 'ab' * 32, 0.01, challenger, king)
    att = sign_dev(tmp_path / 'dev.key', result, 1000)
    config = Config(**{**CHALLENGE_JSON, 'dev_attestation_pubkey': att.signer['pubkey']})
    pr = PullRequest(
        **{'number': 5, 'author': 'alice', 'author_writes': False, 'actor': 'alice', 'actor_writes': False},
        **{'state': 'open', 'reopened': False, 'force_pushed': False, 'commits': 1, 'created_at': datetime.now()},
        body=pr_body('solvers/alice/1', 'baselines/good', 1000),
        changed_files=2,
        files=[ATTESTATION, 'solvers/alice/1/solve'],
        attestation=att.to_json(),
        solver_sha=CHALLENGER,
    )
    repo = Repo('baselines/good', KING, '| round |\n|---|\n| 0 |\n', taken=[], queued=[], unrecorded=[])

    assert submission_error(att, config, CHALLENGER, KING, 1010, 'ab' * 32) is None
    assert decide(pr, repo, lambda seed_block: Chain(1010, 'ab' * 32), config).decision == 'crown'


@pytest.mark.parametrize(
    'add, reason',
    [
        (lambda d: None, None),
        (lambda d: (d / 'a.out').write_bytes(b'\x7fELF\x02\x01\x01\x00'), 'a.out is binary'),
        (lambda d: (d / 'latin1.py').write_bytes('# café'.encode('latin-1')), 'latin1.py is binary'),
        (lambda d: (d / 'big.txt').write_text('x' * (SOURCE_FILE_BYTES + 1)), 'big.txt is 1048577 bytes'),
        (lambda d: (d / 'lib').symlink_to('/usr/lib'), 'lib is a symlink'),
        (lambda d: (d / 'solve').write_text('echo no shebang'), 'solve is not a script'),
        (lambda d: (d / '__pycache__').mkdir() or (d / '__pycache__' / 'x.pyc').write_bytes(b'\0'), None),
    ],
)
def test_a_solver_is_source_only(solver, add, reason):
    add(path := solver('good'))
    error = source_error(dir_files(path))

    assert error.startswith(reason) if reason else error is None
