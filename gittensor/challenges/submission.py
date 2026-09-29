# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A submission: the solver directory as a deterministic ``.tar.gz`` (sorted entries, zeroed times and owners, modes
kept), so the same solver always has the same sha256; the chain commitment ``gt-challenge:<challenge_id>:<sha256>``
(first commit wins ties); and the upload to Hippius S3 when ``HIPPIUS_*`` credentials are set.

TODO: v0 bundles are plaintext. Encrypt to the attested evaluator's key before upload, so a solver stays private
while it reigns.
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import hmac
import io
import os
import tarfile
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from gittensor.challenges.runner import SOLVE

COMMITMENT_PREFIX = 'gt-challenge'
HIPPIUS_ENDPOINT = 'https://s3.hippius.com'
HIPPIUS_REGION = 'decentralized'
UPLOAD_TIMEOUT_S = 300


class SubmissionError(ValueError):
    """The solver directory cannot be submitted."""


@dataclass(frozen=True)
class Bundle:
    challenge_id: str
    data: bytes
    sha256: str

    @property
    def name(self) -> str:
        return f'{self.challenge_id}-{self.sha256}.tar.gz'

    @property
    def commitment(self) -> str:
        return f'{COMMITMENT_PREFIX}:{self.challenge_id}:{self.sha256}'


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    return info.replace(mtime=0, uid=0, gid=0, uname='', gname='', deep=False)


def build_bundle(challenge_id: str, solver_dir: str | Path) -> Bundle:
    solver_dir = Path(solver_dir)
    if not os.access(solver_dir / SOLVE, os.X_OK) or not (solver_dir / SOLVE).is_file():
        raise SubmissionError(f'{solver_dir} has no executable {SOLVE}')
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as gz, tarfile.open(fileobj=gz, mode='w') as tar:
        tar.add(solver_dir, arcname='.', filter=_normalize)  # tarfile recurses in sorted order
    data = raw.getvalue()
    return Bundle(challenge_id, data, hashlib.sha256(data).hexdigest())


def hippius_credentials(env: Mapping[str, str] = os.environ) -> dict | None:
    """``HIPPIUS_ACCESS_KEY``, ``HIPPIUS_SECRET_KEY`` and ``HIPPIUS_BUCKET`` (all required), plus optional
    ``HIPPIUS_ENDPOINT`` and ``HIPPIUS_REGION``; None when any required one is unset."""
    keys = ('HIPPIUS_ACCESS_KEY', 'HIPPIUS_SECRET_KEY', 'HIPPIUS_BUCKET')
    if not all(env.get(k) for k in keys):
        return None
    return {
        'access_key': env['HIPPIUS_ACCESS_KEY'],
        'secret_key': env['HIPPIUS_SECRET_KEY'],
        'bucket': env['HIPPIUS_BUCKET'],
        'endpoint': env.get('HIPPIUS_ENDPOINT') or HIPPIUS_ENDPOINT,
        'region': env.get('HIPPIUS_REGION') or HIPPIUS_REGION,
    }


def upload(bundle: Bundle, creds: dict, now: datetime.datetime | None = None) -> str:
    """PUT the bundle (path-style, SigV4-signed); returns its URL."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    url = f'{creds["endpoint"].rstrip("/")}/{creds["bucket"]}/{bundle.challenge_id}/{bundle.name}'
    request = urllib.request.Request(
        url, data=bundle.data, method='PUT', headers=sigv4_headers(url, bundle, creds, now)
    )
    with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_S):
        pass
    return url


def sigv4_headers(url: str, bundle: Bundle, creds: dict, now: datetime.datetime) -> dict[str, str]:
    parsed = urlparse(url)
    amz_date, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    headers = {'host': parsed.netloc, 'x-amz-content-sha256': bundle.sha256, 'x-amz-date': amz_date}
    signed = ';'.join(sorted(headers))
    canonical = '\n'.join(
        [
            'PUT',
            quote(parsed.path),
            '',
            *(f'{k}:{headers[k]}' for k in sorted(headers)),
            '',
            signed,
            bundle.sha256,
        ]
    )
    scope = f'{day}/{creds["region"]}/s3/aws4_request'
    to_sign = f'AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    key = f'AWS4{creds["secret_key"]}'.encode()
    for part in (day, creds['region'], 's3', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f'AWS4-HMAC-SHA256 Credential={creds["access_key"]}/{scope}, SignedHeaders={signed}, Signature={signature}'
    return {**headers, 'Authorization': auth, 'Content-Type': 'application/gzip'}
