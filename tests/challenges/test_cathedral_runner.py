# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge attest --runner cathedral`` against a fake Cathedral API: what goes up, what is waited for, and
that an attestation is written only when it is signed by the pinned key, names this miner and this solver, and is a
crown. The endpoints are proposed, so the fake is the only server these tests talk to."""

import base64
import io
import json
import shutil
import tarfile

import pytest
import requests
from click.testing import CliRunner

from gittensor.challenges import cathedral_runner
from gittensor.challenges.attestation import Attestation, verify
from gittensor.challenges.cathedral import Author, receipt_error
from gittensor.challenges.checkout import ATTESTATION, CONFIG
from gittensor.challenges.head_to_head import solver_sha
from tests.challenges import cathedral_fake
from tests.challenges.conftest import CHALLENGE_JSON
from tests.challenges.test_cli import cli

IMAGE = 'ghcr.io/o/fake@sha256:' + 'a' * 64
WHO = {'github_login': 'alice', 'github_id': 4242, 'hotkey': '5Alice'}
HASH = 'ab' * 32


class FakeSession:
    """Cathedral's side of the proposed API: accepts a run, reports ``statuses`` in turn, then returns ``attest``."""

    def __init__(self):
        self.headers, self.calls, self.statuses, self.attest = {}, [], ['queued', 'running', 'succeeded'], None

    def request(self, method, url, timeout, json=None, headers=None):
        self.calls.append((method, url, json, headers))
        if method == 'POST':
            body = {'run_id': 'run-1', 'status': 'accepted', 'seed_block': 1000, 'seed_block_hash': HASH, 'attempt': 1}
        else:
            status = self.statuses.pop(0)
            body = {
                'status': status,
                'attestation': self.attest() if status == 'succeeded' else None,
            }
            body['error'] = 'no capacity' if status == 'failed' else None
        return FakeResponse(body)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def result_for(root, crown=True, challenger=None):
    return {
        **{k: CHALLENGE_JSON[k] for k in ('challenge_id', 'module', 'tier', 'margin')},
        **{'n': CHALLENGE_JSON['seeds'], 'seed_block_hash': HASH, 'mean_gain': 0.05, 'crown': crown},
        **{'challenger': {'sha': challenger or solver_sha(root / 'solvers' / 'alice' / '1'), 'valid': 3}},
        **{'king': {'sha': solver_sha(root / 'baselines' / 'good'), 'valid': 3}},
    }


@pytest.fixture
def cathedral(challenge_repo, solver, tmp_path, monkeypatch):
    """A challenge checkout that pins the fake Cathedral key, alice's solver in it, and the fake API."""
    repo = challenge_repo('good')
    config = {**json.loads((repo / CONFIG).read_text()), 'image': IMAGE}
    config['attestation'] = {
        'kind': 'cathedral',
        'min_trust': 'platform',
        'trusted_keys': cathedral_fake.trusted_keys(),
    }
    (repo / CONFIG).write_text(json.dumps(config))
    root = checkout_of(repo, tmp_path)
    shutil.copytree(solver('good'), root / 'solvers' / 'alice' / '1')
    session = FakeSession()
    session.attest = lambda: cathedral_fake.issue(result_for(root), 1000, IMAGE, config, WHO).to_json()
    monkeypatch.chdir(root)
    monkeypatch.setenv('CATHEDRAL_API_KEY', 'test-key')
    monkeypatch.setattr(requests, 'Session', lambda: session)
    monkeypatch.setattr(cathedral_runner.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr('gittensor.challenges.cli.gh_user_id', lambda: 4242)
    monkeypatch.setattr('gittensor.challenges.cli.gh_user', lambda: 'alice')
    return root, session, config


def checkout_of(repo, tmp_path):
    from tests.challenges.test_cli import clone, publish

    (work := tmp_path / 'work').mkdir()
    clone(publish(repo), work / repo.name)
    return work / repo.name


def attest(*args):
    return CliRunner().invoke(cli, ['challenge', 'attest', '--runner', 'cathedral', '--login', 'alice', *args])


def test_a_crown_run_on_cathedral_writes_the_checked_attestation(cathedral):
    root, session, _ = cathedral

    result = attest()

    assert result.exit_code == 0, result.output
    att = Attestation.from_json((root / ATTESTATION).read_text())
    assert att.signer['kind'] == 'cathedral' and att.result['challenger']['sha'] == solver_sha(root / 'solvers/alice/1')
    assert 'seed block 1000, attempt 1' in result.output and 'run-1: succeeded' in result.output
    post = session.calls[0]
    assert (post[0], post[1], post[3]['Idempotency-Key'] != '') == (
        'POST',
        'https://cathedral.computer/v1/challenge-runs',
        True,
    )
    assert session.headers['Authorization'] == 'Bearer test-key'


def test_the_request_carries_the_solver_the_king_and_who_is_asking(cathedral):
    root, session, _ = cathedral
    attest()

    body = session.calls[0][2]
    config = json.loads(base64.b64decode(body['challenge']['config_b64']))
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(body['challenger']['tar_b64']))).getnames()
    assert (body['challenger']['path'], body['king']['path']) == ('solvers/alice/1', 'baselines/good')
    assert names == ['solve'] and config['challenge_id'] == 'fake-echo' and len(body['challenge']['main_commit']) == 40
    assert body['submitter'] == {'github_login': 'alice', 'github_id': 4242, 'hotkey': None, 'hotkey_signature': None}
    assert body['seed'] == {'mode': 'latest_finalized', 'network': 'finney'}


def test_the_same_files_make_the_same_tar(tmp_path, solver):
    one, two = solver('good'), tmp_path / 'copy'
    shutil.copytree(one, two)

    assert cathedral_runner.solver_tar_b64(one) == cathedral_runner.solver_tar_b64(two)


def test_without_an_api_key_nothing_is_sent(cathedral, monkeypatch):
    monkeypatch.delenv('CATHEDRAL_API_KEY')

    result = attest()

    assert result.exit_code != 0 and 'CATHEDRAL_API_KEY' in result.output and not cathedral[1].calls


def test_resume_waits_for_the_run_without_starting_another(cathedral):
    root, session, _ = cathedral

    result = attest('--resume', 'run-1')

    assert result.exit_code == 0, result.output
    assert {method for method, *_ in session.calls} == {'GET'} and (root / ATTESTATION).exists()


@pytest.mark.parametrize('status', ['failed', 'expired'])
def test_a_failed_or_expired_run_writes_nothing_and_says_why(cathedral, status):
    root, session, _ = cathedral
    session.statuses = ['running', status]

    result = attest()

    assert result.exit_code != 0 and f'run run-1 {status}' in result.output and not (root / ATTESTATION).exists()


def test_a_run_that_is_not_a_crown_writes_nothing(cathedral):
    root, session, config = cathedral
    session.attest = lambda: cathedral_fake.issue(result_for(root, crown=False), 1000, IMAGE, config, WHO).to_json()

    result = attest()

    assert result.exit_code != 0 and 'no crown' in result.output and not (root / ATTESTATION).exists()


@pytest.mark.parametrize(
    'who, challenger, reason',
    [
        ({**WHO, 'github_id': 7}, None, 'not alice (4242)'),
        (WHO, 'f' * 40, 'not this solver and KING'),
    ],
)
def test_an_attestation_for_someone_else_or_another_solver_is_not_written(cathedral, who, challenger, reason):
    root, session, config = cathedral
    result_doc = result_for(root, challenger=challenger)
    session.attest = lambda: cathedral_fake.issue(result_doc, 1000, IMAGE, config, who).to_json()

    result = attest()

    assert result.exit_code != 0 and reason in result.output and not (root / ATTESTATION).exists()


def test_an_attestation_signed_by_a_key_challenge_json_does_not_pin_is_not_written(cathedral):
    root, session, config = cathedral
    config['attestation']['trusted_keys'] = {}
    (root / CONFIG).write_text(json.dumps(config))
    shutil_git_commit(root)

    result = attest()

    assert result.exit_code != 0 and 'challenge.json pins' in result.output and not (root / ATTESTATION).exists()


def shutil_git_commit(root):
    from tests.challenges.test_cli import git

    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'drop the key')
    git(root, 'push', '-q', 'upstream', 'HEAD:main')


def test_the_p0_quote_binding_is_checked_and_a_swapped_claim_is_refused(cathedral):
    root, _, config = cathedral
    att = cathedral_fake.issue(result_for(root), 1000, IMAGE, config | {'seeds': 3}, WHO)
    policy = config['attestation']
    fake_config = type(
        'C',
        (),
        {'attestation': policy, **{k: config[k] for k in ('challenge_id', 'module', 'tier', 'seeds')}, 'image': IMAGE},
    )
    assert (
        verify(att, None, policy['trusted_keys'])
        and receipt_error(att, fake_config, Author('alice', 4242, '5Alice')) is None
    )

    doc = json.loads(att.to_json())
    doc['signer']['receipt']['tee']['e2e_pubkey_b64'] = base64.b64encode(b'x' * 32).decode()
    swapped = Attestation.from_json(json.dumps(doc))
    assert 'signature' in (receipt_error(swapped, fake_config) or '')
