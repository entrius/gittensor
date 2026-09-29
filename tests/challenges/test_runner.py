# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The runner: seeds from the seed block hash, every way a solver can fail or misbehave lands as a 0-scored seed with its
reason, nothing it starts outlives it, and without a sandbox nothing runs at all."""

import hashlib
from pathlib import Path

import pytest

from gittensor.challenges import runner
from gittensor.challenges.runner import SeedResult, evaluate
from tests.challenges import fake_challenge
from tests.challenges.conftest import requires_sandbox


def run(solver_dir: Path, n: int = 1) -> list[SeedResult]:
    [results] = evaluate(fake_challenge, 'small', 'block-0xabc', n, [solver_dir])
    return results


@requires_sandbox
def test_each_seed_is_generated_once_from_the_seed_block_hash_and_every_solver_runs_on_it(solver, monkeypatch):
    seeds, generate = [], fake_challenge.generate

    def spy(seed, *args):
        seeds.append(seed)
        generate(seed, *args)

    monkeypatch.setattr(fake_challenge, 'generate', spy)

    results = evaluate(fake_challenge, 'small', 'block-0xabc', 2, [solver('good'), solver('good'), solver('crash')])

    assert seeds == [hashlib.sha256(f'block-0xabc:{i}'.encode()).digest() for i in range(2)]
    assert [[r.score for r in rs] for rs in results] == [[1.0, 1.0], [1.0, 1.0], [0.0, 0.0]]


@requires_sandbox
@pytest.mark.parametrize(
    'name, reason',
    [
        ('slow', 'timed out after 1 s'),
        ('crash', 'exit 3: boom'),
        ('garbage', 'wrong answer'),
        ('hog', 'MemoryError'),
        ('link', 'output holds a link or special file: answer.txt'),
        ('online', 'Network is unreachable'),
    ],
)
def test_a_failing_solver_scores_zero_per_seed_never_raises(solver, name, reason):
    assert all(not r.valid and r.score == 0.0 and reason in r.reason for r in run(solver(name), 2))


def cmdlines():
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            yield path.read_bytes()
        except OSError:  # gone since the glob
            pass


@requires_sandbox
@pytest.mark.parametrize(
    'name, reason',
    [
        ('hidden-link', 'output holds a link or special file: answer.txt'),
        ('hidden-dir-link', 'output holds a link or special file: l'),
        ('deep-nest', 'output unreadable: [Errno 36] File name too long'),
    ],
)
def test_output_cannot_hide_a_link_behind_permissions_or_depth(solver, name, reason):
    [seed] = run(solver(name))

    assert seed.score == 0.0 and seed.reason.startswith(reason)


@requires_sandbox
def test_the_solvers_python3_is_the_evaluators_with_its_packages(solver):
    assert run(solver('evaluator-python'))[0].score == 1.0


@requires_sandbox
def test_the_solver_is_pinned_to_solver_cpus(solver, monkeypatch):
    monkeypatch.setattr(runner, 'SOLVER_CPUS', 1)

    assert run(solver('one-cpu'))[0].score == 1.0


@requires_sandbox
def test_a_process_the_solver_detaches_dies_with_the_sandbox(solver):
    assert run(solver('escapee'))[0].score == 1.0

    assert b'sleep\x0031.4159\x00' not in set(cmdlines())


@requires_sandbox
def test_a_solve_that_is_not_executable_scores_zero(solver):
    path = solver('good')
    (path / 'solve').chmod(0o644)

    [seed] = run(path)

    assert (seed.score, 'Permission denied' in seed.reason) == (0.0, True)


def test_without_a_sandbox_nothing_runs(solver, monkeypatch):
    monkeypatch.setattr(runner, 'sandbox_error', lambda: 'bwrap is not installed')

    [seed] = run(solver('good'))

    assert (seed.score, seed.reason) == (0.0, 'sandbox unavailable: bwrap is not installed')
