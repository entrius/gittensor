# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The maintainer's verdict from plain facts: each check's failure decides, in order; all passing crowns."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from gittensor.challenges.attestation import sign_dev
from gittensor.challenges.verify import CLA_TEXT, Chain, Config, PullRequest, Repo, decide

KING = [0.8, 1.0, 1.2, 0.9, 1.1, 1.0, 0.7, 1.3] * 4
HASH, KING_SHA, SOLVER_SHA = 'ab' * 32, 'k' * 40, 'c' * 40
RESULT = {
    'challenge_id': 'intents-batch',
    'module': 'gt_challenge_intents',
    'tier': 'standard',
    'n': 32,
    'margin': 0.01,
    'seed_block_hash': HASH,
    'challenger': {'sha': SOLVER_SHA, 'scores': [s * 1.05 for s in KING], 'valid': 32},
    'king': {'sha': KING_SHA, 'scores': KING, 'valid': 32},
    'crown': True,
}
CONFIG = Config(
    challenge_id='intents-batch',
    module='gt_challenge_intents',
    image=None,
    tier='standard',
    seeds=32,
    margin=0.01,
    suspicious_gain=0.25,
    freshness_blocks=150,
    crown_label='intents:r{round}:crown',
    maintainers=['entrius'],
    dev_attestation_pubkey=None,
)
PR = PullRequest(
    number=7,
    author='miner',
    author_writes=False,
    actor='miner',
    actor_writes=False,
    state='open',
    ever_closed=False,
    commits=1,
    files=['attestation.json', 'solvers/miner/1/solve', 'solvers/miner/1/lib/util.py'],
    created_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
    body=f'My solver.\r\n\r\n- [X] {CLA_TEXT}\r\n',
    solver_sha=SOLVER_SHA,
)
LEADERBOARD = '| round | solver |\n|---|---|\n| 0 | baselines/cow |\n| 1 | solvers/a/1 |\n'
REPO = Repo(king='solvers/a/1', king_sha=KING_SHA, leaderboard=LEADERBOARD, taken=['solvers/miner/2'], earlier_open=[])


@pytest.fixture(scope='module')
def key(tmp_path_factory):
    return tmp_path_factory.mktemp('keys') / 'dev.key'


def judge(key, pr=None, repo=None, chain=None, result=None, config=None, seed_block=1000):
    att = sign_dev(key, RESULT | (result or {}), seed_block)
    pr = replace(PR, attestation=att.to_json(), **(pr or {}))
    config = replace(CONFIG, **({'dev_attestation_pubkey': att.signer['pubkey']} | (config or {})))
    return decide(pr, replace(REPO, **(repo or {})), replace(Chain(1100, HASH), **(chain or {})), config)


def scored(factor):
    return {'challenger': {'sha': SOLVER_SHA, 'scores': [s * factor for s in KING], 'valid': 32}}


@pytest.mark.parametrize(
    'changes, decision, check',
    [
        ({'pr': {'actor': 'Entrius'}}, 'ignore', 'actor'),
        ({'pr': {'actor': 'helper', 'actor_writes': True}}, 'ignore', 'actor'),
        ({'pr': {'author': 'entrius', 'actor': 'bystander'}}, 'ignore', 'eligible'),
        ({'pr': {'ever_closed': True}}, 'ignore', 'eligible'),
        ({'pr': {'commits': 2}}, 'close', 'one commit'),
        ({'pr': {'body': f'- [ ] {CLA_TEXT}'}}, 'close', 'cla'),
        ({'repo': {'earlier_open': [5]}}, 'wait', 'queue'),
        ({'pr': {'files': [*PR.files, 'README.md']}}, 'close', 'scope'),
        ({'pr': {'files': ['attestation.json', 'solvers/miner/2/solve']}}, 'close', 'scope'),
        ({'config': {'dev_attestation_pubkey': '00' * 32}}, 'close', 'signature'),
        ({'seed_block': 900}, 'close', 'seed'),
        ({'chain': {'seed_block_hash': 'cd' * 32}}, 'close', 'seed'),
        ({'repo': {'king_sha': 'f' * 40}}, 'close', 'king'),
        ({'pr': {'solver_sha': 'e' * 40}}, 'close', 'challenger'),
        ({'result': {'tier': 'small'}}, 'close', 'config'),
        ({'result': {'challenger': {**RESULT['challenger'], 'valid': 31}}}, 'close', 'validity'),
        ({'result': scored(1.005)}, 'close', 'crown rule'),
        ({'result': scored(1.5)}, 'needs_review', 'suspicious'),
    ],
)
def test_the_first_failing_check_decides(key, changes, decision, check):
    verdict = judge(key, **changes)

    assert (verdict.decision, verdict.checks[-1].name, verdict.checks[-1].ok) == (decision, check, False)
    assert verdict.reason == verdict.checks[-1].detail and verdict.round is None


def test_a_moved_king_closes_as_stale(key):
    assert judge(key, repo={'king_sha': 'f' * 40}).reason.startswith('stale: re-run against the new KING')


def test_all_checks_passing_crown_the_next_round_with_the_recomputed_bound(key):
    verdict = json.loads(judge(key).to_json())

    assert verdict['decision'] == 'crown' and verdict['round'] == 2
    assert verdict['gain'] == pytest.approx(0.05) and verdict['lower_99'] == pytest.approx(0.05)
    assert all(check['ok'] for check in verdict['checks'])
