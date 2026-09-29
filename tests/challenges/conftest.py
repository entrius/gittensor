# The MIT License (MIT)
# Copyright © 2026 Entrius

import json
import shutil

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
    'hidden-link': 'ln -s "$1/number.txt" "$2/answer.txt"; chmod 111 "$2"',
    'hidden-dir-link': 'mkdir "$2/sub"; ln -s /instance "$2/sub/l"; chmod 111 "$2/sub"; cat "$1/number.txt" > "$2/answer.txt"',
    'deep-nest': 'cd "$2"; exec python3 -c "import os\nfor _ in range(2100): os.mkdir(\'a\'); os.chdir(\'a\')"',
    'evaluator-python': 'python3 -c "import click" && cat "$1/number.txt" > "$2/answer.txt"',
    'one-cpu': 'python3 -c "import os, sys; sys.exit(len(os.sched_getaffinity(0)) != 1)" && cat "$1/number.txt" > "$2/answer.txt"',
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


CHALLENGE_JSON = {
    'challenge_id': 'fake-echo',
    'module': FAKE_MODULE,
    'tier': 'small',
    'seeds': 3,
    'margin': 0.01,
    'freshness_blocks': 150,
    'dev_attestation_pubkey': None,
}


@pytest.fixture
def challenge_repo(tmp_path, solver):
    """``challenge_repo(king)``: a challenge checkout whose ``KING`` is ``baselines/<king>``, a ``SOLVERS`` script."""

    def make(king: str = 'good'):
        root = tmp_path / 'gt-challenge-fake'
        (root / '.gittensor').mkdir(parents=True)
        (root / '.gittensor' / 'challenge.json').write_text(json.dumps(CHALLENGE_JSON))
        shutil.copytree(solver(king), root / 'baselines' / king)
        (root / 'KING').write_text(f'baselines/{king}\n')
        (root / 'README.md').write_text('# gt-challenge-fake\n\nEcho the number back.\n\n## More\n')
        return root

    return make
