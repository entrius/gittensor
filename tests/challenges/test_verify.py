# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The maintainer's verdict from plain facts: each check's failure decides, in order; all passing crowns."""

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import click
import pytest
from click.testing import CliRunner

from gittensor.challenges import verify as verify_module
from gittensor.challenges.attestation import sign_dev
from gittensor.challenges.checkout import SourceFile, source_error
from gittensor.challenges.verify import CLA_TEXT, Chain, Config, PullRequest, Repo, block_at, decide, verify_command

KING = [0.8, 1.0, 1.2, 0.9, 1.1, 1.0, 0.7, 1.3] * 4
HASH, KING_SHA, SOLVER_SHA = 'ab' * 32, 'k' * 40, 'c' * 40
FILES = [
    SourceFile('solve', '100755', 20, lambda: b'#!/bin/sh\nexec ./fast'),
    SourceFile('lib/util.py', '100644', 9, lambda: b'X = 1\n'),
]
BINARY = SourceFile('fast', '100755', 8, lambda: b'\x7fELF\x02\x01\x01\x00')
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
    reopened=False,
    force_pushed=False,
    commits=1,
    created_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
    body=f'My solver.\r\n\r\n- [X] {CLA_TEXT}\r\n',
    changed_files=3,
    files=['attestation.json', 'solvers/miner/1/solve', 'solvers/miner/1/lib/util.py'],
    solver_sha=SOLVER_SHA,
    solver_files=FILES,
)
LEADERBOARD = '| round | solver | pr |\n|---|---|---|\n| 0 | baselines/cow | - |\n| 1 | solvers/a/1 | #3 |\n'
REPO = Repo(
    king='solvers/a/1', king_sha=KING_SHA, leaderboard=LEADERBOARD, taken=['solvers/miner/2'], queued=[], unrecorded=[]
)


@pytest.fixture(scope='module')
def key(tmp_path_factory):
    return tmp_path_factory.mktemp('keys') / 'dev.key'


def judge(key, pr=None, repo=None, chain=None, result=None, config=None, seed_block=1000):
    att = sign_dev(key, RESULT | (result or {}), seed_block)
    pr = replace(PR, attestation=att.to_json(), **(pr or {}))
    config = replace(CONFIG, **({'dev_attestation_pubkey': att.signer['pubkey']} | (config or {})))
    return decide(pr, replace(REPO, **(repo or {})), lambda _: replace(Chain(1100, HASH), **(chain or {})), config)


def scored(factor):
    return {'challenger': {'sha': SOLVER_SHA, 'scores': [s * factor for s in KING], 'valid': 32}}


@pytest.mark.parametrize(
    'changes, decision, check',
    [
        ({'pr': {'actor': 'Entrius'}}, 'ignore', 'actor'),
        ({'pr': {'actor': 'helper', 'actor_writes': True}}, 'ignore', 'actor'),
        ({'pr': {'author': 'entrius', 'actor': 'bystander'}}, 'ignore', 'eligible'),
        ({'pr': {'reopened': True}}, 'ignore', 'eligible'),
        ({'pr': {'base': 'dev'}}, 'ignore', 'eligible'),
        ({'pr': {'state': 'merged', 'author': 'entrius', 'actor': 'bystander'}}, 'ignore', 'unrecorded crown'),
        ({'pr': {'state': 'merged', 'number': 3}}, 'ignore', 'unrecorded crown'),
        ({'pr': {'commits': 2}}, 'close', 'one commit'),
        ({'pr': {'force_pushed': True}}, 'close', 'one commit'),
        ({'pr': {'draft': True}}, 'wait', 'ready'),
        ({'pr': {'body': f'- [ ] {CLA_TEXT}'}}, 'close', 'cla'),
        ({'repo': {'queued': [5]}}, 'wait', 'queue'),
        ({'repo': {'unrecorded': [9]}}, 'wait', 'queue'),
        ({'pr': {'files': [*PR.files, 'README.md'], 'changed_files': 4}}, 'close', 'scope'),
        ({'pr': {'renamed_from': ['README.md']}}, 'close', 'scope'),
        ({'pr': {'changed_files': 3001}}, 'close', 'scope'),
        ({'pr': {'files': ['attestation.json', 'solvers/miner/2/solve']}}, 'close', 'scope'),
        ({'pr': {'solver_files': [*FILES, BINARY]}}, 'close', 'source'),
        ({'pr': {'solver_truncated': True}}, 'close', 'source'),
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


def test_the_wait_says_which_prs_are_open_and_which_merged_crowns_to_record(key):
    verdict = judge(key, repo={'queued': [5], 'unrecorded': [9]})

    assert verdict.reason == 'waiting on open PRs [5]; merged crowns to record [9]'


def test_a_moved_king_closes_as_stale(key):
    assert judge(key, repo={'king_sha': 'f' * 40}).reason.startswith('stale: re-run against the new KING')


def test_all_checks_passing_crown_the_next_round_with_the_recomputed_bound(key):
    verdict = json.loads(judge(key).to_json())

    assert verdict['decision'] == 'crown' and verdict['round'] == 2
    assert verdict['gain'] == pytest.approx(0.05) and verdict['lower_99'] == pytest.approx(0.05)
    assert all(check['ok'] for check in verdict['checks'])


def test_a_merged_submission_not_yet_on_the_leaderboard_is_crowned_to_finish_it(key):
    verdict = judge(key, pr={'state': 'merged', 'actor': 'miner'})

    assert (verdict.decision, verdict.round, verdict.checks[-1].name) == ('crown', 2, 'crown')


def test_block_at_finds_the_last_block_at_or_before_a_time_without_passing_the_head():
    t0 = datetime(2026, 9, 29, tzinfo=timezone.utc)

    class Subtensor:
        def get_current_block(self):
            return 1000

        def get_timestamp(self, block):
            assert block <= 1000
            return t0 + timedelta(seconds=12 * block + (block % 3))  # ~12 s blocks with jitter

    chain = Subtensor()
    assert [block_at(chain, t0 + timedelta(seconds=s)) for s in (6001, 6002, 6011, 6012)] == [499, 500, 500, 501]
    assert block_at(chain, t0 + timedelta(days=1)) == 1000


def test_verify_registers_as_a_click_command():
    group = click.Group()
    group.add_command(verify_command)

    result = CliRunner().invoke(group, ['verify', '--help'])

    assert result.exit_code == 0 and '--apply' in result.output


def test_a_pr_tree_with_a_symlink_or_a_binary_blob_is_not_source_only(monkeypatch):
    tree = [
        {'path': 'solve', 'mode': '100755', 'type': 'blob', 'sha': 's', 'size': 10},
        {'path': 'lib', 'mode': '040000', 'type': 'tree', 'sha': 't'},
        {'path': 'lib/fast.so', 'mode': '100644', 'type': 'blob', 'sha': 'b', 'size': 8},
    ]
    blobs = {'s': b'#!/bin/sh\n', 'b': b'\x7fELF\x02\x01\x01\x00'}
    responses = {'repos/o/r/git/trees/x?recursive=1': {'tree': tree, 'truncated': False}}
    responses |= {f'repos/o/r/git/blobs/{k}': {'content': base64.b64encode(v).decode()} for k, v in blobs.items()}
    monkeypatch.setattr(verify_module, 'api', responses.__getitem__)
    link = {'path': 'lib/fast.so', 'mode': '120000', 'type': 'blob', 'sha': 'b', 'size': 4}

    def error():
        files, truncated = verify_module.tree_files('o/r', 'x')
        return 'truncated' if truncated else source_error(files) or ''

    assert error().startswith('lib/fast.so is binary')
    tree[2] = link
    assert error().startswith('lib/fast.so is a symlink')
    responses['repos/o/r/git/trees/x?recursive=1']['truncated'] = True
    assert error() == 'truncated'


def test_a_truncated_tree_listing_closes_the_pr(key):
    verdict = judge(key, pr={'solver_truncated': True})

    assert verdict.reason == 'solvers/miner/1 is not source only: tree listing truncated'
