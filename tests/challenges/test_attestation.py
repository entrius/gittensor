# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``attestation.json``: a dev signature round-trips through JSON; any tampered field, or another key, fails."""

import dataclasses

import pytest

from gittensor.challenges.attestation import Attestation, sign_dev, verify

RESULT = {'challenger': {'sha': 'c' * 40}, 'king': {'sha': 'k' * 40}, 'crown': True, 'lower_99': 0.02}


def test_a_dev_attestation_round_trips_and_its_key_file_is_private(tmp_path):
    key = tmp_path / 'keys' / 'dev.key'
    att = sign_dev(key, RESULT, 123, 'img@sha256:ab')

    loaded = Attestation.from_json(att.to_json())

    assert loaded == att and verify(loaded, att.signer['pubkey'])
    assert key.stat().st_mode & 0o777 == 0o600
    assert sign_dev(key, RESULT, 123, 'img@sha256:ab').signer == att.signer  # the key is reused


@pytest.mark.parametrize(
    'change',
    [
        {'result': {**RESULT, 'crown': False}},
        {'seed_block': 124},
        {'image': None},
        {'signature': '00' * 64},
        {'signature': 'not hex'},
    ],
)
def test_a_tampered_attestation_fails(tmp_path, change):
    att = sign_dev(tmp_path / 'dev.key', RESULT, 123, 'img@sha256:ab')

    assert not verify(dataclasses.replace(att, **change), att.signer['pubkey'])


def test_another_key_or_no_accepted_key_fails(tmp_path):
    att = sign_dev(tmp_path / 'dev.key', RESULT, 123)
    other = sign_dev(tmp_path / 'other.key', RESULT, 123)

    assert not verify(att, other.signer['pubkey']) and not verify(att, None)
    assert not verify(dataclasses.replace(att, signer=other.signer), att.signer['pubkey'])


def test_polaris_is_not_wired_and_says_where_to_read(tmp_path):
    att = dataclasses.replace(sign_dev(tmp_path / 'dev.key', RESULT, 123), signer={'kind': 'polaris'})

    with pytest.raises(NotImplementedError, match='docs.fr0ntierx.com/attestation'):
        verify(att, None)


def test_from_json_rejects_what_is_not_an_attestation():
    for text in ('[]', '{}', 'nope', '{"result": {}, "seed_block": "1", "image": null, "signer": {}, "signature": ""}'):
        with pytest.raises(ValueError):
            Attestation.from_json(text)
