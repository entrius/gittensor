# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The proof slot on its own: docker lines, the two phases, the fail-closed default, and docker/proof/proof_job.py
(not a package: loaded by path) against a fake staged binary."""

import importlib.util
import json
import stat
from pathlib import Path

import pytest

from gittensor.controller.checks.scrape import GpuInfo
from gittensor.controller.proof import slot
from tests.controller.conftest import FakeProof, container_for, job_responder, passing_runner

JOB = Path(__file__).resolve().parents[2] / 'docker' / 'proof' / 'proof_job.py'


def gpu(uuid: str) -> GpuInfo:
    return GpuInfo(uuid, 'NVIDIA GeForce RTX 5090', '580.65.06', 32607, 575.0, 575.0, 600.0, '00000000:01:00.0', '12.0')


GPU_A, GPU_B = gpu('GPU-aaaa'), gpu('GPU-bbbb')


def test_docker_lines_pin_the_card_and_quote_arguments():
    line = slot.create_command('entrius/gt-proof:dev', 'GPU-1', 'gt-proof-0', ['--', '--challenge', 'ab cd'])
    assert line == (
        'docker create --gpus="device=GPU-1" --name gt-proof-0 --label io.gittensor.proof.uuid=GPU-1 '
        "entrius/gt-proof:dev -- --challenge 'ab cd'"
    )
    assert slot.start_command('abc') == 'docker start -a abc'
    assert slot.remove_command(['a', 'b c']) == "docker rm -f a 'b c'"
    assert slot.image_ref('r', 'dev', '') == 'r:dev' and slot.image_ref('r', 'dev', 'sha256:1') == 'r@sha256:1'


def test_stage_then_fire_is_two_phases_across_cards():
    runner = passing_runner()
    proof = FakeProof()
    staged = slot.stage_box(runner, [GPU_A, GPU_B], proof, 'img:test')
    assert set(staged.containers) == {'GPU-aaaa', 'GPU-bbbb'} and staged.version == 'fake-1'
    assert [c for c in runner.calls if c.startswith('docker start')] == []  # nothing started yet
    cards = slot.fire_box(runner, [GPU_A, GPU_B], proof, staged, clock=iter([0.0, 0.0, 1.0, 1.0]).__next__)
    assert [c['uuid'] for c in cards] == ['GPU-aaaa', 'GPU-bbbb'] and all(c['passed'] for c in cards)
    assert {c['command'] for c in cards} == {
        f'docker start -a {container_for("GPU-aaaa")}',
        f'docker start -a {container_for("GPU-bbbb")}',
    }
    # a card that was never staged cannot be fired
    (card,) = slot.fire_box(runner, [gpu('GPU-cccc')], proof, staged)
    assert not card['passed'] and card['reason'] == 'not staged'


def test_probe_box_cleans_up_and_reports():
    runner = passing_runner()
    result = slot.probe_box(runner, [GPU_A], FakeProof(), 'img:test')
    assert result.passed and result.failures == [] and result.provider == 'fake-1'
    assert runner.calls[-1] == f'docker rm -f {container_for("GPU-aaaa")}'
    result = slot.probe_box(passing_runner(job=job_responder(uuid='GPU-other')), [GPU_A], FakeProof(), 'img:test')
    assert not result.passed and result.failures == ['GPU-aaaa: answered from GPU-other, not GPU-aaaa']
    assert slot.probe_box(runner, [], FakeProof()).error == 'no GPUs to prove'


def test_unconfigured_proof_fails_closed_everywhere():
    p = slot.UnconfiguredProof()
    with pytest.raises(slot.ProofUnavailable, match='no GPU proof provider'):
        p.stage(passing_runner(), slot.BoxIdentity(('GPU-1',), '5090'), 'img', 10.0)
    with pytest.raises(slot.ProofUnavailable):
        p.start_command(slot.StagedProof('x', {}), 'GPU-1')
    assert not p.judge(None, 'GPU-1', '{}', 0.0, None).passed and p.cleanup_command(slot.StagedProof('x', {})) is None
    result = slot.probe_box(passing_runner(), [GPU_A], p)
    assert not result.passed and 'no GPU proof provider' in result.error and result.cards == []


# --- docker/proof/proof_job.py --------------------------------------------------------------------------------------


@pytest.fixture
def job():
    spec = importlib.util.spec_from_file_location('proof_job', JOB)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_binary(tmp_path, body: str) -> str:
    path = tmp_path / 'gt_proof'
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


ANSWER = {'uuid': 'GPU-aaaa', 'filled_bytes': 30771471974, 'wall_ms': 1512.4, 'speed': 41.2, 'challenge': 'c1'}


def test_job_passes_arguments_through_and_adds_the_outer_clock(job, tmp_path):
    binary = fake_binary(tmp_path, f'echo "$@" >&2; echo \'{json.dumps(ANSWER)}\'')
    code, payload = job.run(['--challenge', 'c1'], timeout=10.0, binary=binary)
    assert code == 0 and payload['challenge'] == 'c1' and payload['wall_ms'] == 1512.4
    assert payload['job_version'] == job.VERSION and payload['run_ms'] >= 0


def test_job_failures(job, tmp_path):
    code, payload = job.run([], timeout=10.0, binary=str(tmp_path / 'missing'))
    assert code == 1 and 'nothing staged' in payload['error']
    binary = fake_binary(tmp_path, 'echo \'{"error":"not enough free VRAM: 12345"}\'; exit 3')
    code, payload = job.run([], timeout=10.0, binary=binary)
    assert code == 1 and payload['exit'] == 3 and 'not enough free VRAM' in payload['error']
    code, payload = job.run([], timeout=10.0, binary=fake_binary(tmp_path, 'echo not-json'))
    assert code == 1 and 'no JSON' in payload['error']
    code, payload = job.run([], timeout=10.0, binary=fake_binary(tmp_path, 'echo "[1]"'))
    assert code == 1 and 'not an object' in payload['error']
    code, payload = job.run([], timeout=0.2, binary=fake_binary(tmp_path, 'sleep 2'))
    assert code == 1 and 'timeout' in payload['error']


def test_job_main_prints_json_and_exit_code(job, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(job, 'BIN', fake_binary(tmp_path, f"echo '{json.dumps(ANSWER)}'"))
    assert job.main(['--', '--challenge', 'c1']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['uuid'] == 'GPU-aaaa' and out['job_version'] == job.VERSION
    monkeypatch.setattr(job, 'BIN', str(tmp_path / 'nope'))
    assert job.main([]) == 1 and 'nothing staged' in json.loads(capsys.readouterr().out)['error']


def test_job_is_stdlib_only_and_the_image_ships_no_binary():
    src = JOB.read_text()
    assert 'import requests' not in src and 'http.server' not in src
    dockerfile = (JOB.parent / 'Dockerfile').read_text()
    assert 'COPY docker/proof/proof_job.py' in dockerfile and 'gt_gemm' not in dockerfile.split('# The public kernel')[
        0
    ].replace('kernel/gt_gemm.cu', '')
    assert 'nvcc' not in dockerfile  # nothing is compiled into the base image
