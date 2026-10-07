# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge attest --runner cathedral``: the official run happens on Cathedral (cathedral.computer), not on the
miner's machine, so the miner never has a shell where the result is made.

The API below is PROPOSED. The endpoints ship with Cathedral's P0 and may change before then; every name here is in one
place so it is easy to follow them. A run is one job: the solver and the KING go up as tarballs, Cathedral pins the
seed block when it accepts the job, runs the pinned evaluator image with no network, and returns the finished
``attestation.json`` with its receipt.

    POST {url}/challenge-runs          202 {run_id, status, seed_block, seed_block_hash, attempt}
    GET  {url}/challenge-runs/{id}     {status: queued|running|succeeded|failed|expired, attestation, error}
"""

from __future__ import annotations

import io
import os
import tarfile
import time
import uuid
from base64 import b64encode
from collections.abc import Callable
from pathlib import Path

import requests

from gittensor.challenges.checkout import dir_files

API_URL = 'https://cathedral.computer/v1'
URL_ENV, KEY_ENV = 'CATHEDRAL_API_URL', 'CATHEDRAL_API_KEY'
POLL_SECONDS = 30
TIMEOUT_SECONDS = 8 * 3600  # a standard run is up to ~6 h; the freshness window is what really bounds it
REQUEST_TIMEOUT = 60
FINAL = ('succeeded', 'failed', 'expired')


class CathedralError(Exception):
    """The run could not be made or did not succeed; the message says why."""


def solver_tar_b64(solver_dir: Path) -> str:
    """The solver's source files as a tar (names sorted, modes 644 or 755, no owners or times), base64. The same
    files give the same bytes, and Cathedral recomputes the git tree sha from it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for f in dir_files(solver_dir):
            info = tarfile.TarInfo(f.path)
            info.size, info.mode = f.size, 0o755 if f.mode == '100755' else 0o644
            tar.addfile(info, io.BytesIO(f.read()))
    return b64encode(buffer.getvalue()).decode()


def run_request(
    repo: str, main_commit: str, config_text: str, solver: tuple[str, Path], king: tuple[str, Path], who: dict
) -> dict:
    """The body of ``POST /challenge-runs``. ``solver`` and ``king`` are (path in the repo, local directory); ``who``
    is {github_login, github_id}: Cathedral looks the miner's hotkey up itself."""
    return {
        'challenge': {'repo': repo, 'main_commit': main_commit, 'config_b64': b64encode(config_text.encode()).decode()},
        'challenger': {'path': solver[0], 'tar_b64': solver_tar_b64(solver[1])},
        'king': {'path': king[0], 'tar_b64': solver_tar_b64(king[1])},
        'submitter': {**who, 'hotkey': None, 'hotkey_signature': None},
        'seed': {'mode': 'latest_finalized', 'network': 'finney'},
    }


class Client:
    def __init__(self, url: str, api_key: str, session: requests.Session | None = None):
        self.url, self.session = url.rstrip('/'), session or requests.Session()
        self.session.headers['Authorization'] = f'Bearer {api_key}'

    @classmethod
    def from_env(cls) -> Client:
        if not (api_key := os.environ.get(KEY_ENV)):
            raise CathedralError(f'set {KEY_ENV} to your Cathedral API key (the official run is billed to it)')
        return cls(os.environ.get(URL_ENV, API_URL), api_key)

    def call(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.session.request(method, f'{self.url}{path}', timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            raise CathedralError(f'Cathedral {method} {path} failed: {e}') from e

    def create_run(self, body: dict) -> dict:
        """One new run. The idempotency key is per call: a retry of a lost response must use ``--resume`` instead, so
        that a second run, which counts as another attempt, is always the miner's choice."""
        return self.call('POST', '/challenge-runs', json=body, headers={'Idempotency-Key': str(uuid.uuid4())})

    def get_run(self, run_id: str) -> dict:
        return self.call('GET', f'/challenge-runs/{run_id}')


def wait_for_run(
    client: Client,
    run_id: str,
    on_status: Callable[[str], None] = lambda status: None,
    sleep: Callable[[float], None] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Poll until the run is final and return its ``attestation.json`` text. Raises ``CathedralError`` if the run
    failed or expired, the response is not a finished attestation, or ``timeout`` passes."""
    sleep, deadline, seen = sleep or time.sleep, time.monotonic() + timeout, None
    while True:
        run = client.get_run(run_id)
        status = run.get('status')
        if status != seen:
            on_status(str(status))
            seen = status
        if status == 'succeeded':
            if not isinstance(attestation := run.get('attestation'), str):
                raise CathedralError(f'run {run_id} succeeded without an attestation')
            return attestation
        if status in FINAL:
            raise CathedralError(f'run {run_id} {status}: {run.get("error") or "no reason given"}')
        if time.monotonic() > deadline:
            raise CathedralError(
                f'run {run_id} is still {status} after {timeout:g}s: `--resume {run_id}` to keep waiting'
            )
        sleep(POLL_SECONDS)
