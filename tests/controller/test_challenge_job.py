# The MIT License (MIT)
# Copyright © 2025 Entrius

"""docker/challenge/challenge_job.py (not a package: loaded by path) against a fake gt_challenge binary."""

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

JOB = Path(__file__).resolve().parents[2] / 'docker' / 'challenge' / 'challenge_job.py'


@pytest.fixture
def job():
    spec = importlib.util.spec_from_file_location('challenge_job', JOB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_binary(tmp_path, body: str) -> str:
    path = tmp_path / 'gt_challenge'
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


ANSWER = {
    'seed': 12345,
    'device': 0,
    'uuid': 'GPU-4f2a6b8c-1d3e-4a5b-9c7d-0e1f2a3b4c5d',
    'name': 'NVIDIA GeForce RTX 5090',
    'driver': '580.65.06',
    'sm_count': 170,
    'vram_total': 34190524416,
    'vram_free_before': 33561116672,
    'filled_bytes': 30771471974,
    'fill_ratio': 0.9,
    'dim': 1024,
    'matrices': 512,
    'iters': 3,
    'digest': 'ab' * 32,
    'wall_ms': 1512.4,
}


def test_build_args(job):
    args = job.build_args(12345, 0.9, 3, 0, 1024, 512)
    assert args == [
        '--seed',
        '12345',
        '--fill-ratio',
        '0.9',
        '--iters',
        '3',
        '--device',
        '0',
        '--dim',
        '1024',
        '--matrices',
        '512',
    ]
    assert job.build_args(1, 0.0, 99, 1, 256, 2)[5] == '20'  # iters clamped
    with pytest.raises(ValueError):
        job.build_args(1, 1.5, 3, 0, 1024, 512)


def test_run_success_adds_version_and_outer_clock(job, tmp_path):
    binary = fake_binary(tmp_path, f"echo '{json.dumps(ANSWER)}'")
    code, payload = job.run(job.build_args(12345, 0.9, 3, 0, 1024, 512), timeout=10.0, binary=binary)
    assert code == 0 and payload['digest'] == 'ab' * 32 and payload['wall_ms'] == 1512.4
    assert payload['job_version'] == job.VERSION and payload['run_ms'] >= 0


def test_run_failures(job, tmp_path):
    binary = fake_binary(tmp_path, 'echo \'{"device":0,"error":"not enough free VRAM: 12345"}\'; exit 3')
    code, payload = job.run(['--seed', '1'], timeout=10.0, binary=binary)
    assert code == 1 and payload['exit'] == 3 and 'not enough free VRAM' in payload['error']
    code, payload = job.run(['--seed', '1'], timeout=10.0, binary=fake_binary(tmp_path, 'echo not-json'))
    assert code == 1 and 'no JSON' in payload['error']
    code, payload = job.run(['--seed', '1'], timeout=10.0, binary=str(tmp_path / 'missing'))
    assert code == 1 and 'cannot run' in payload['error']
    code, payload = job.run(['--seed', '1'], timeout=0.2, binary=fake_binary(tmp_path, 'sleep 2'))
    assert code == 1 and 'timeout' in payload['error']


def test_main_prints_json_and_exit_code(job, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(job, 'BIN', fake_binary(tmp_path, f"echo '{json.dumps(ANSWER)}'"))
    assert job.main(['--seed', '12345']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['seed'] == 12345 and out['digest'] == 'ab' * 32 and out['job_version'] == job.VERSION
    assert job.main(['--seed', '1', '--fill-ratio', '2']) == 2
    assert 'fill_ratio' in json.loads(capsys.readouterr().out)['error']


def test_job_is_executable_stdlib_only():
    src = JOB.read_text()
    assert os.access(JOB, os.R_OK) and 'import requests' not in src and 'http.server' not in src
