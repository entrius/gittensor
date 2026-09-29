# The MIT License (MIT)
# Copyright © 2026 Entrius

import json

import pytest

from gittensor.challenges.runner import sandbox_error

FAKE_MODULE = 'tests.challenges.fake_challenge'
requires_sandbox = pytest.mark.skipif(bool(sandbox_error()), reason=f'no sandbox here: {sandbox_error()}')

SOLVERS = {
    'good': 'cat "$1/number.txt" > "$2/answer.txt"',
    'slow': 'sleep 30',
    'crash': 'echo boom >&2; exit 3',
    'garbage': 'head -c 4096 /dev/urandom > "$2/answer.txt"',
    'hog': 'exec python3 -c "bytearray(1 << 30)"',
    'link': 'ln -s "$1/number.txt" "$2/answer.txt"',
    'online': 'exec python3 -c "import socket; socket.create_connection((\'1.1.1.1\', 53), timeout=2)"',
    'escapee': 'setsid -f sleep 31.4159 </dev/null >/dev/null 2>&1; cat "$1/number.txt" > "$2/answer.txt"',
}


@pytest.fixture
def solver(tmp_path):
    """``solver(name)``: a submission directory whose ``solve`` is the ``SOLVERS[name]`` shell script."""

    def make(name: str):
        path = tmp_path / 'solvers' / name
        path.mkdir(parents=True, exist_ok=True)
        (path / 'solve').write_text(f'#!/bin/sh\n{SOLVERS[name]}\n')
        (path / 'solve').chmod(0o755)
        return path

    return make


@pytest.fixture
def registry_path(tmp_path):
    path = tmp_path / 'challenges.json'
    entry = {'module': FAKE_MODULE, 'repo': 'entrius/gt-challenge-fake', 'version': '0.1.0', 'tier': 'small'}
    path.write_text(json.dumps({'fake-echo': {**entry, 'seeds': 2, 'dethrone_margin': 0.01, 'emission_share': 0.5}}))
    return path
