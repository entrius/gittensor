# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A Cathedral receipt inside ``attestation.json``: the sample (synthetic scores, synthetic quote, a key derived from a
public string) verifies, and every field a forger would change is refused. Nothing here came from hardware."""

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from Crypto.Signature import eddsa

from gittensor.challenges.attestation import Attestation, digest, public_hex, verify
from gittensor.challenges.cathedral import (
    CLAIMS_DOMAIN,
    QUOTE_BODY,
    QUOTE_HEADER,
    Author,
    pinned_error,
    receipt_bytes,
    receipt_error,
)
from gittensor.challenges.checkout import SourceFile, submission_error
from gittensor.challenges.head_to_head import canonical
from gittensor.challenges.verify import CLA_TEXT, Chain, Config, PullRequest, Repo, decide

SAMPLE = json.loads(Path(__file__).with_name('fixtures').joinpath('cathedral_sample_attestation.json').read_text())
SAMPLE_SEED = hashlib.sha256(b'SAMPLE KEY - cathedral challenge receipt v1 - DO NOT TRUST').digest()
KEY_ID = SAMPLE['signer']['key_id']
PUBKEY = bytes.fromhex(SAMPLE['signer']['pubkey'])
MEASUREMENT = SAMPLE['signer']['receipt']['tee']['image_measurement']
AUTHOR = Author('sample-miner', 100000001, '5SAMPLEHotkeyNotARealSS58AddressxxxxxxxxxxxxxxxX')
POLICY = {
    'kind': 'cathedral',
    'min_trust': 'platform',
    'tdx_image_measurements': [MEASUREMENT],
    'trusted_keys': {
        KEY_ID: {
            'algorithm': 'ed25519',
            'public_key_base64': base64.b64encode(PUBKEY).decode(),
            'status': 'active',
            'valid_from': '2026-10-01T00:00:00.000000Z',
            'valid_until': '2027-10-01T00:00:00.000000Z',
        }
    },
}
CONFIG = Config(
    challenge_id='intents-batch',
    module='gt_challenge_intents',
    image=SAMPLE['image'],
    tier='standard',
    seeds=1000,
    margin=0.01,
    suspicious_gain=0.25,
    freshness_blocks=600,
    crown_label='intents:r{round}:crown',
    maintainers=['entrius'],
    dev_attestation_pubkey=None,
    attestation=POLICY,
)


def sample() -> dict:
    return copy.deepcopy(SAMPLE)


def resigned(doc: dict) -> Attestation:
    """The sample with the receipt signed again by the sample key: only the edited field is wrong, not the signature."""
    key = eddsa.import_private_key(SAMPLE_SEED)
    receipt = doc['signer']['receipt']
    body = {k: v for k, v in receipt.items() if k != 'signature'}
    receipt['signature']['value_base64'] = base64.b64encode(
        eddsa.new(key, 'rfc8032').sign(receipt_bytes(body))
    ).decode()
    return Attestation.from_json(canonical(doc))


def rebound(doc: dict) -> Attestation:
    """The sample after an edit to its claims, with the quote's report data and the signature made again, so the edit
    is the only thing a check can object to (what a holder of the Cathedral key could issue)."""
    receipt = doc['signer']['receipt']
    tee, claims = receipt['tee'], receipt['claims']
    commitment = hashlib.sha256(CLAIMS_DOMAIN + receipt_bytes(claims)).digest()
    quote = base64.b64decode(tee['quote_b64'])
    start = QUOTE_HEADER + sum(size for name, size in QUOTE_BODY if name != 'report_data')
    assert tee['binding'] == 'rd-claims-v1'  # the sample's: the commitment itself, then zeros
    quote = quote[:start] + commitment + quote[start + 32 :]
    tee['quote_b64'], tee['quote_sha256'] = base64.b64encode(quote).decode(), hashlib.sha256(quote).hexdigest()
    return resigned(doc)


def refusal(att: Attestation, config: Config = CONFIG, author: Author | None = AUTHOR) -> str:
    return receipt_error(att, config, author) or 'accepted'


def test_the_sample_signature_and_receipt_verify_with_the_pinned_key():
    att = Attestation.from_json(json.dumps(SAMPLE))

    assert verify(att, None, POLICY['trusted_keys'])
    assert receipt_error(att, CONFIG, AUTHOR) is None
    assert att.to_json() == canonical(SAMPLE)  # gittensor's own canonical bytes are unchanged by the receipt


def test_a_cathedral_signer_needs_a_pinned_key_and_a_dev_signer_has_no_receipt(tmp_path):
    att = Attestation.from_json(json.dumps(SAMPLE))
    trusted = POLICY['trusted_keys']
    revoked = {KEY_ID: {**trusted[KEY_ID], 'status': 'revoked'}}
    other = {KEY_ID: {**trusted[KEY_ID], 'public_key_base64': base64.b64encode(bytes(32)).decode()}}

    assert not verify(att, None) and not verify(att, None, {}) and not verify(att, None, revoked)
    assert not verify(att, None, other)
    assert not verify(replace(att, signer={**att.signer, 'key_id': 'unknown'}), None, trusted)
    assert not verify(replace(att, signer={**att.signer, 'pubkey': '00' * 32}), None, trusted)
    dev = replace(att, signer={'kind': 'dev', 'pubkey': 'ab' * 32})
    assert receipt_error(dev, replace(CONFIG, attestation={}), AUTHOR) is None  # no Cathedral policy: no receipt
    assert 'accepts only a Cathedral attestation' in refusal(dev)  # a policy for Cathedral: no dev signer at all


@pytest.mark.parametrize(
    'edit',
    [
        lambda a: a['result']['challenger'].update(valid=999),
        lambda a: a.update(seed_block=a['seed_block'] + 1),
        lambda a: a.update(image=None),
    ],
)
def test_editing_the_attested_bytes_breaks_both_signatures(edit):
    doc = sample()
    edit(doc)
    att = Attestation.from_json(json.dumps(doc))

    assert not verify(att, None, POLICY['trusted_keys'])
    assert 'receipt signature' in refusal(att) or 'does not match' in refusal(att)


def test_a_forged_score_with_a_fresh_receipt_signature_still_fails_the_hashes():
    doc = sample()
    doc['result']['challenger']['scores'][0] += 1.0

    assert 'attestation digest' in refusal(resigned(doc)) and 'result hash' in refusal(resigned(doc))


@pytest.mark.parametrize(
    'edit, reason',
    [
        (lambda r: r['claims']['run'].update(seed_block=1), 'seed block'),
        (lambda r: r['claims']['run'].update(seed_block_hash='0' * 64), 'seed block hash'),
        (lambda r: r['claims']['run'].update(exit_code=1), 'exit code'),
        (lambda r: r['claims']['evaluator'].update(egress='open'), 'egress'),
        (lambda r: r['claims']['evaluator'].update(image='ghcr.io/other@sha256:' + '1' * 64), 'image'),
        (lambda r: r['claims']['challenge'].update(seeds=10), 'seeds'),
        (lambda r: r['claims']['submission'].update(challenger_tree_sha='f' * 40), 'challenger sha'),
        (lambda r: r['claims']['outputs'].update(crown=False), 'crown'),
        (lambda r: r['tee'].update(image_measurement='tdx-image-sha256:' + '0' * 64), 'does not carry|measurement'),
        (lambda r: r['tee'].update(binding='rd-other'), 'unknown quote binding'),
        (lambda r: r['tee'].update(quote_sha256='0' * 64), 'no TDX quote'),
        (lambda r: r.update(schema='cathedral_challenge_receipt_v2'), 'no cathedral_challenge_receipt_v1'),
        (lambda r: r.update(extra=1), 'missing or unknown fields'),
        (lambda r: r['claims'].update(extra=1), 'missing or unknown fields'),
        (lambda r: r.update(issued_at='2027-10-02T00:00:00.000000Z'), 'not a trusted key'),
        (lambda r: r['claims']['run'].update(attempt=1.5), 'holds a float'),
    ],
)
def test_a_receipt_edited_and_signed_again_is_refused_by_the_field_it_changed(edit, reason):
    doc = sample()
    edit(doc['signer']['receipt'])

    assert any(word in refusal(resigned(doc)) for word in reason.split('|'))


@pytest.mark.parametrize(
    'attempt, cap, accepted',
    [(1, None, True), (999, None, True), (3, 3, True), (4, 3, False), (0, None, False), (True, None, False)],
)
def test_attempts_above_max_attempts_are_refused_and_the_count_must_be_a_real_count(attempt, cap, accepted):
    doc = sample()
    doc['signer']['receipt']['claims']['run']['attempt'] = attempt
    config = replace(CONFIG, attestation={**POLICY, **({} if cap is None else {'max_attempts': cap})})

    reason = refusal(rebound(doc), config)
    assert (reason == 'accepted') == accepted
    if cap is not None and attempt == 4:
        assert 'attempt 4 is over max_attempts 3' in reason


def test_a_max_attempts_that_is_not_a_whole_number_refuses_everything():
    att = Attestation.from_json(json.dumps(SAMPLE))

    assert 'max_attempts' in refusal(att, replace(CONFIG, attestation={**POLICY, 'max_attempts': '3'}))


@pytest.mark.parametrize(
    'edit, reason',
    [
        (lambda r: r['trust'].update(claimed_level='quantum'), 'trust level'),
        (lambda r: r['signer'].update(key_id='another-key'), 'receipt signer key'),
        (lambda r: r['signer'].update(kind='dev'), 'receipt signer kind'),
        (lambda r: r['signer'].update(algorithm='rsa'), 'receipt signer algorithm'),
        (lambda r: r['claims']['challenge'].update(repo='someone/else'), 'repo'),
    ],
)
def test_the_receipt_fields_that_were_only_required_are_checked(edit, reason):
    doc = sample()
    edit(doc['signer']['receipt'])
    config = replace(CONFIG, attestation={**POLICY, 'repo': 'entrius/gt-challenge-intents'})

    assert reason in refusal(resigned(doc), config)
    assert refusal(Attestation.from_json(json.dumps(SAMPLE)), config) == 'accepted'


def test_who_the_receipt_was_issued_to_must_be_the_pr_author_and_the_hotkey():
    att = Attestation.from_json(json.dumps(SAMPLE))

    assert 'not other-miner' in refusal(att, author=replace(AUTHOR, login='other-miner'))
    assert 'not sample-miner (7)' in refusal(att, author=replace(AUTHOR, github_id=7))
    assert 'hotkey' in refusal(att, author=replace(AUTHOR, hotkey='5Other'))
    assert refusal(att, author=replace(AUTHOR, login='Sample-Miner')) != 'accepted'  # the solver path is exact
    assert refusal(att, author=None) == 'accepted'
    assert refusal(att, author=replace(AUTHOR, hotkey=None)) == 'accepted'


def test_hardware_trust_is_refused_until_the_intel_chain_is_checked():
    att = Attestation.from_json(json.dumps(SAMPLE))
    config = replace(CONFIG, attestation={**POLICY, 'min_trust': 'hardware'})

    assert 'Intel quote chain' in refusal(att, config)


def test_a_runner_measurement_missing_from_challenge_json_is_refused():
    att = Attestation.from_json(json.dumps(SAMPLE))
    config = replace(CONFIG, attestation={**POLICY, 'tdx_image_measurements': ['tdx-image-sha256:' + '0' * 64]})

    assert 'is not in challenge.json' in refusal(att, config)


def test_the_digest_the_legacy_signature_covers_is_gittensors_own():
    att = Attestation.from_json(json.dumps(SAMPLE))

    assert digest(att.result, att.seed_block, att.image).hex() == att.signer['receipt']['claims']['attestation_digest']


DEV_SEED = hashlib.sha256(b'a dev key that challenge.json also lists').digest()


def relabeled_as_dev() -> tuple[Attestation, str]:
    """Alice's Cathedral attestation with ``signer`` rewritten to a dev key and the legacy signature made with it. The
    receipt is dropped, as a copycat would. Returns the attestation and the dev public key."""
    key = eddsa.import_private_key(DEV_SEED)
    pub = public_hex(key)
    att = Attestation.from_json(json.dumps(SAMPLE))
    signed = eddsa.new(key, 'rfc8032').sign(digest(att.result, att.seed_block, att.image)).hex()
    return replace(att, signer={'kind': 'dev', 'pubkey': pub}, signature=signed), pub


def test_a_cathedral_attestation_relabeled_as_dev_is_refused_even_when_the_dev_key_is_accepted():
    dev, pub = relabeled_as_dev()
    config = replace(CONFIG, dev_attestation_pubkey=pub)

    assert verify(dev, pub)  # the dev signature itself is genuine: only the policy can refuse it
    assert 'accepts only a Cathedral attestation' in refusal(dev, config)
    assert 'not acceptable' in (submission_error(dev, config, 'a' * 40, 'b' * 40, 1, 'c' * 64, AUTHOR) or '')

    assert [(c.name, c.ok) for c in judge(attestation=dev.to_json(), config=config).checks][-2:] == [
        ('image', True),
        ('receipt', False),
    ]
    assert judge(attestation=dev.to_json(), config=config).decision == 'close'


def test_a_polaris_policy_is_held_to_the_cathedral_signer_too():
    dev, pub = relabeled_as_dev()
    config = replace(CONFIG, dev_attestation_pubkey=pub, attestation={**POLICY, 'kind': 'polaris'})

    assert 'accepts only a Cathedral attestation' in refusal(dev, config)
    assert refusal(Attestation.from_json(json.dumps(SAMPLE)), config) == 'accepted'


def test_a_challenge_with_no_usable_pinned_key_is_known_before_any_run_is_paid_for():
    placeholder = {KEY_ID: {**POLICY['trusted_keys'][KEY_ID], 'public_key_base64': 'REPLACE-WITH-THE-PUBLIC-KEY'}}
    revoked = {KEY_ID: {**POLICY['trusted_keys'][KEY_ID], 'status': 'revoked'}}

    assert pinned_error(CONFIG) is None
    assert pinned_error(replace(CONFIG, attestation={**POLICY, 'kind': 'polaris'})) is None
    assert 'does not name Cathedral' in (pinned_error(replace(CONFIG, attestation={})) or '')
    assert 'does not name Cathedral' in (pinned_error(replace(CONFIG, attestation={'kind': 'dev'})) or '')
    for keys in (placeholder, revoked, {}, None, ['x']):
        assert 'no usable Cathedral key' in (
            pinned_error(replace(CONFIG, attestation={**POLICY, 'trusted_keys': keys})) or ''
        )


def judge(config: Config = CONFIG, **pr_changes):
    """The maintainer's verdict on a PR carrying the sample, from plain facts."""
    files = [SourceFile('solve', '100755', 20, lambda: b'#!/bin/sh\nexec ./fast')]
    pr = PullRequest(
        number=7,
        author=AUTHOR.login,
        author_writes=False,
        actor=AUTHOR.login,
        actor_writes=False,
        state='open',
        reopened=False,
        force_pushed=False,
        commits=1,
        created_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
        body=f'- [x] {CLA_TEXT}',
        changed_files=2,
        files=['attestation.json', 'solvers/sample-miner/1/solve'],
        solver_sha=SAMPLE['result']['challenger']['sha'],
        solver_files=files,
        author_hotkey=lambda: AUTHOR.hotkey,
        author_id=AUTHOR.github_id,
        attestation=json.dumps(SAMPLE),
    )
    repo = Repo('baselines/cow', SAMPLE['result']['king']['sha'], '| round |\n|---|\n| 0 |\n', [], [], [])
    chain = Chain(SAMPLE['seed_block'] + 50, SAMPLE['result']['seed_block_hash'])
    return decide(replace(pr, **pr_changes), repo, lambda _: chain, config)


def test_the_maintainer_crowns_the_sample_pr_and_closes_one_opened_by_someone_else():
    crowned = judge()

    assert (crowned.decision, crowned.checks[-1].name) == ('crown', 'crown')
    assert [c.name for c in crowned.checks if c.name == 'receipt'] == ['receipt']

    copied = judge(author='copycat', author_id=9, files=['attestation.json', 'solvers/copycat/1/solve'])
    assert (copied.decision, copied.checks[-1].name) == ('close', 'receipt')
