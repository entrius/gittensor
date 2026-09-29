# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The bundle is deterministic (same solver, same sha256, whatever the file times) and carries the commitment; the
Hippius upload signs as botocore does."""

import datetime
import hashlib
import os

import pytest

from gittensor.challenges.submission import Bundle, SubmissionError, build_bundle, sigv4_headers


def test_the_same_solver_bundles_to_the_same_sha256(solver):
    path = solver('good')
    first = build_bundle('fake-echo', path)
    os.utime(path / 'solve', (0, 0))

    assert build_bundle('fake-echo', path) == first
    assert first.commitment == f'gt-challenge:fake-echo:{first.sha256}'
    (path / 'solve').write_text('#!/bin/sh\nexit 1\n')
    assert build_bundle('fake-echo', path).sha256 != first.sha256


def test_a_directory_without_an_executable_solve_is_refused(solver):
    path = solver('good')
    (path / 'solve').chmod(0o644)

    with pytest.raises(SubmissionError, match='no executable solve'):
        build_bundle('fake-echo', path)


def test_the_upload_signature_matches_botocore():
    data = b'hello bundle'
    bundle = Bundle('routing-cvrptw', data, hashlib.sha256(data).hexdigest())
    creds = {'access_key': 'AKID', 'secret_key': 'SECRET/key+x', 'region': 'decentralized'}
    url = f'https://s3.hippius.com/my-bucket/routing-cvrptw/{bundle.name}'
    now = datetime.datetime(2026, 9, 29, 12, 0, 0, tzinfo=datetime.timezone.utc)

    auth = sigv4_headers(url, bundle, creds, now)['Authorization']

    assert auth.endswith('Signature=655ddc05c59d628e555b074abcbfcd017f59c21abdab99838a5326088f44ef6e')  # botocore's
