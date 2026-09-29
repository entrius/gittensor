# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The runner: seeds from the public seed, every way a solver can fail or misbehave lands as a 0-scored seed with its
reason, nothing it starts outlives it, and without a sandbox nothing runs at all."""

from pathlib import Path

import pytest

from gittensor.challenges import runner
from gittensor.challenges.runner import derive_seeds, evaluate
from tests.challenges import fake_challenge
from tests.challenges.conftest import requires_sandbox


@requires_sandbox
def test_a_correct_solver_scores_one_on_seeds_derived_from_the_public_seed(solver):
    result = evaluate(fake_challenge, solver('good'), 'small', 'block-0xabc', 3)

    assert result.score == 1.0
    assert all(r.valid for r in result.results)
    assert [r.seed for r in result.results] == [s.hex() for s in derive_seeds('block-0xabc', 3)]
    assert len(set(derive_seeds('block-0xabc', 3))) == 3


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
    result = evaluate(fake_challenge, solver(name), 'small', 'block-0xabc', 2)

    assert result.score == 0.0
    assert all(not r.valid and r.score == 0.0 and reason in r.reason for r in result.results)


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
    [seed] = evaluate(fake_challenge, solver(name), 'small', 'block-0xabc', 1).results

    assert seed.score == 0.0 and seed.reason.startswith(reason)


@requires_sandbox
def test_the_solvers_python3_is_the_evaluators_with_its_packages(solver):
    assert evaluate(fake_challenge, solver('evaluator-python'), 'small', 'block-0xabc', 1).score == 1.0


@requires_sandbox
def test_the_solver_is_pinned_to_solver_cpus_and_the_count_is_recorded(solver, monkeypatch):
    monkeypatch.setattr(runner, 'SOLVER_CPUS', 1)

    result = evaluate(fake_challenge, solver('one-cpu'), 'small', 'block-0xabc', 1)

    assert (result.score, result.cpus) == (1.0, 1)


@requires_sandbox
def test_a_process_the_solver_detaches_dies_with_the_sandbox(solver):
    assert evaluate(fake_challenge, solver('escapee'), 'small', 'block-0xabc', 1).score == 1.0

    assert b'sleep\x0031.4159\x00' not in set(cmdlines())


@requires_sandbox
def test_a_solve_that_is_not_executable_scores_zero(solver):
    path = solver('good')
    (path / 'solve').chmod(0o644)

    [seed] = evaluate(fake_challenge, path, 'small', 'block-0xabc', 1).results

    assert (seed.score, 'Permission denied' in seed.reason) == (0.0, True)


def test_without_a_sandbox_nothing_runs(solver, monkeypatch):
    monkeypatch.setattr(runner, 'sandbox_error', lambda: 'bwrap is not installed')

    [seed] = evaluate(fake_challenge, solver('good'), 'small', 'block-0xabc', 1).results

    assert (seed.score, seed.reason) == (0.0, 'sandbox unavailable: bwrap is not installed')
