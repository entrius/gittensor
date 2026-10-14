# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge attest --runner cathedral``: the official run happens on Cathedral (cathedral.computer), not on the
miner's machine, so the miner never has a shell where the result is made.

The API below is PROPOSED. The endpoints ship with Cathedral's P0 and may change before then; every name here is in one
place so it is easy to follow them. A run is one job: the solver and the KING go up as tarballs, Cathedral pins the
seed block when it accepts the job, runs the pinned evaluator image with no network, and returns the finished
``attestation.json`` with its receipt.

    POST {url}/challenge-runs          202 {run_id, status, seed_block, seed_block_hash, attempt}
    GET  {url}/challenge-runs/{id}     {status: queued|running|succeeded|failed|expired, seed_block, attestation, error}

Cathedral counts attempts per (GitHub id, challenge, round) and writes the count into the receipt; the maintainer's
optional ``attestation.max_attempts`` in challenge.json caps it, so a second run of an edited solver is still attempt 2.
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

from gittensor.challenges.checkout import SLACK_BLOCKS, dir_files

API_URL = 'https://cathedral.computer/v1'
URL_ENV, KEY_ENV = 'CATHEDRAL_API_URL', 'CATHEDRAL_API_KEY'
POLL_SECONDS = 30
POLL_RETRIES = 8  # failed polls in a row before giving up; with BACKOFF that is about 6 minutes of Cathedral being down
BACKOFF_SECONDS, BACKOFF_CAP_SECONDS = 5, 120
BLOCK_SECONDS = 12
SUBMIT_MARGIN_BLOCKS = 50  # kept out of the wait for `gitt challenge submit` and the PR (about 10 minutes)
REQUEST_TIMEOUT = 60
ERROR_BODY_CHARS = 200
FINAL = ('succeeded', 'failed', 'expired')


class CathedralError(Exception):
    """The run could not be made or did not succeed; the message says why."""


class TransientError(CathedralError):
    """A failure that is worth trying again: the connection, a timeout, 429 or a 5xx."""


def resume_command(run_id: str) -> str:
    return f'gitt challenge attest --runner cathedral --resume {run_id}'


def solver_tar_b64(solver_dir: Path) -> str:
    """The solver's files as a tar (names sorted, modes 644 or 755, no owners or times), base64. A symlink is a tar
    symlink entry, which Cathedral hashes as git's 120000 blob of the link text, as ``solver_sha`` does here, so the
    tree sha is the same. A link to nothing, or a special file, is an error before anything is sent. The same files
    give the same bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for f in dir_files(solver_dir):
            info = tarfile.TarInfo(f.path)
            if (path := solver_dir / f.path).is_symlink():
                if not path.exists():
                    raise CathedralError(f'{f.path} is a symlink to nothing ({os.readlink(path)}): nothing was sent')
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, os.readlink(path), 0o777
                tar.addfile(info)
                continue
            if f.mode == 'special':
                raise CathedralError(f'{f.path} is a special file: nothing was sent')
            info.size, info.mode = f.size, 0o755 if f.mode == '100755' else 0o644
            tar.addfile(info, io.BytesIO(f.read()))
    return b64encode(buffer.getvalue()).decode()


def fresh_seconds(seed_block: int, freshness_blocks: int, current_block: int) -> float:
    """Seconds left in which a run on ``seed_block`` can finish and still be submitted: the freshness window from the
    seed block, less the slack ``submit`` keeps and ``SUBMIT_MARGIN_BLOCKS`` for the PR, as of ``current_block``."""
    return (seed_block + freshness_blocks - SLACK_BLOCKS - SUBMIT_MARGIN_BLOCKS - current_block) * BLOCK_SECONDS


def run_request(
    repo: str,
    main_commit: str,
    config_text: str,
    solver: tuple[str, Path],
    king: tuple[str, Path],
    who: dict,
    network: str = 'finney',
) -> dict:
    """The body of ``POST /challenge-runs``. ``solver`` and ``king`` are (path in the repo, local directory); ``who``
    is {github_login, github_id}: Cathedral looks the miner's hotkey up itself."""
    return {
        'challenge': {'repo': repo, 'main_commit': main_commit, 'config_b64': b64encode(config_text.encode()).decode()},
        'challenger': {'path': solver[0], 'tar_b64': solver_tar_b64(solver[1])},
        'king': {'path': king[0], 'tar_b64': solver_tar_b64(king[1])},
        'submitter': {**who, 'hotkey': None, 'hotkey_signature': None},
        'seed': {'mode': 'latest_finalized', 'network': network},
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
        what = f'Cathedral {method} {path}'
        try:
            response = self.session.request(method, f'{self.url}{path}', timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()
            body = response.json()
        except requests.HTTPError as e:
            code, text = getattr(e.response, 'status_code', 0), (getattr(e.response, 'text', '') or '').strip()
            detail = f': {text[:ERROR_BODY_CHARS]}' if text else ''
            error = TransientError if code == 429 or code >= 500 else CathedralError
            raise error(f'{what} failed: {e}{detail}') from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f'{what} failed: {e}') from e
        except (requests.RequestException, ValueError) as e:
            raise CathedralError(f'{what} failed: {e}') from e
        if not isinstance(body, dict):
            raise CathedralError(f'{what} answered with {type(body).__name__}, not a JSON object')
        return body

    def create_run(self, body: dict) -> dict:
        """One new run. The idempotency key is per call: a retry of a lost response must use ``--resume`` instead, so
        that a second run, which counts as another attempt, is always the miner's choice."""
        return self.call('POST', '/challenge-runs', json=body, headers={'Idempotency-Key': str(uuid.uuid4())})

    def get_run(self, run_id: str) -> dict:
        return self.call('GET', f'/challenge-runs/{run_id}')


def wait_for_run(
    client: Client,
    run_id: str,
    timeout: float,
    on_status: Callable[[str], None] = lambda status: None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Poll until the run is final and return its ``attestation.json`` text. A poll that fails for a transient reason
    is tried again with a growing pause, up to ``POLL_RETRIES`` in a row. Raises ``CathedralError`` if the run failed
    or expired, the response is not a finished attestation, or ``timeout`` passes; whenever the run may still be going
    the message carries the exact ``--resume`` command, because starting another run is another attempt."""
    sleep, deadline, seen, failures = sleep or time.sleep, time.monotonic() + timeout, None, 0
    keeps_going = f'the run keeps going on Cathedral: `{resume_command(run_id)}` to wait for it'
    while True:
        try:
            run = client.get_run(run_id)
            failures = 0
        except TransientError as e:
            if (failures := failures + 1) >= POLL_RETRIES:
                raise CathedralError(f'{e}; gave up after {failures} tries in a row, {keeps_going}') from e
            pause = min(BACKOFF_SECONDS * 2 ** (failures - 1), BACKOFF_CAP_SECONDS)
        except CathedralError as e:
            raise CathedralError(f'{e}; {keeps_going}') from e
        else:
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
            pause = POLL_SECONDS
        if time.monotonic() + pause > deadline:
            raise CathedralError(
                f'run {run_id} is not done {timeout:g}s after it began, the most that still leaves time to submit '
                f'before its seed block goes stale; {keeps_going} (a stale result cannot be submitted)'
            )
        sleep(pause)
