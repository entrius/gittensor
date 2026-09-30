# The MIT License (MIT)
# Copyright © 2026 Entrius

"""Run solvers on N fresh instances: per seed, ``generate`` once -> for each solver in turn, ``./solve <instance>
<output>`` -> ``check``.

Seeds come from a seed block hash: ``sha256(f'{seed_block_hash}:{i}')``, so anyone can replay a score. ``generate``
writes into private dirs (instance and secret, each its own ``mkdtemp``); each solver gets copies in a third: the
instance (read-only), an empty output dir, and its own directory (a fresh copy per seed and solver, so no state
carries). ``check`` reads the private originals plus the output, and output holding a link or a special file scores 0.

A solver is source only (``checkout.source_error``); one that compiles ships a ``build`` script at its root. It runs
once per solver, before any seed, in the solver's own directory (in place: callers pass a private snapshot, hashed
before the build) under the same sandbox with ``BUILD_TIME_LIMIT_S``, ``BUILD_MEMORY_MB`` and ``BUILD_TMPFS_MB``,
untimed for scoring. What it writes there is what every seed's copy runs. A build that fails, times out or leaves the
dir over ``BUILD_OUTPUT_BYTES`` scores every seed 0 ("build failed").

The solver runs under bubblewrap: new user, pid, net, ipc and uts namespaces (no network), a read-only root with
``/usr`` and the loader, size-capped ``/tmp`` and ``/dev/shm``, and the evaluator's own Python read-only at the same
paths (its prefixes and every ``sys.path`` directory, never one holding the temp dir where the secrets live), so
``python3`` imports the challenge package and its deps. Nothing else of the host is mounted. The environment is only
``PATH=<sys.prefix>/bin:/usr/bin:/bin``, ``HOME=/work``, ``TMPDIR=/tmp`` and ``LANG=C.UTF-8``. Limits: the tier's wall
time (the sandbox is killed, and every process in its pid namespace with it), ``RLIMIT_AS`` at the tier's
``memory_mb`` (address space, not RSS), ``RLIMIT_FSIZE``, ``RLIMIT_NPROC``, and pinned to the first ``SOLVER_CPUS``
available CPUs (fewer where fewer exist; the count used is recorded with every evaluation). Writes to ``/output`` and
``/work`` and the memory of several processes are bounded only per file and per process until the attested container
adds a cgroup.
Without a working ``bwrap`` every seed scores 0 ("sandbox unavailable"): never an unsandboxed run. A timeout, crash,
garbage or unreadable output, or a ``check`` that raises scores 0 for that seed, never an exception.

Same-uid namespaces are defense in depth, not the trust anchor: that is the attested evaluator image (Polaris).
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

SOLVE = 'solve'
BUILD = 'build'
BUILD_TIME_LIMIT_S = 300
BUILD_MEMORY_MB = 4096
BUILD_TMPFS_MB = 512  # each of the build's /tmp and /dev/shm
BUILD_OUTPUT_BYTES = 1 << 30  # the built solver dir, copied for every seed
REASON_CHARS = 200
LOG_TAIL_CHARS = 160  # the end of the solver's output, after the 'exit N: ' prefix
FSIZE_BYTES = 1 << 30
NPROC = 512
SOLVER_CPUS = 4  # time budgets mean something only on a fixed CPU count
HOST_ROOTS = ('/bin', '/sbin', '/lib', '/lib32', '/lib64')  # a symlink into /usr on merged-/usr hosts
HOST_FILES = ('/etc/ld.so.cache', '/etc/alternatives', '/etc/localtime')
ENV = {'PATH': f'{sys.prefix}/bin:/usr/bin:/bin', 'HOME': '/work', 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8'}


@dataclass(frozen=True)
class SeedResult:
    valid: bool
    score: float
    seconds: float
    reason: str = ''


def derive_seeds(seed_block_hash: str, n: int) -> list[bytes]:
    return [hashlib.sha256(f'{seed_block_hash}:{i}'.encode()).digest() for i in range(n)]


@functools.cache
def python_runtime() -> tuple[str, ...]:
    """The evaluator's Python for the sandbox, parents first: its prefixes, the interpreter's install dir and every
    ``sys.path`` directory; not what ``/usr`` already covers, nor any directory holding the temp dir."""
    tmp = os.path.realpath(tempfile.gettempdir())
    install = os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))
    keep = set()
    for path in {sys.prefix, sys.base_prefix, install, *sys.path}:
        real = os.path.realpath(path)
        if not os.path.isabs(path) or not os.path.isdir(path):
            continue
        if os.path.commonpath([real, '/usr']) == '/usr' or os.path.commonpath([real, tmp]) == real:
            continue
        keep.add(path)
    return tuple(sorted(keep, key=len))


def sandbox(tmpfs_mb: int) -> list[str]:
    """The bwrap command up to the solver's own mounts, with ``/tmp`` and ``/dev/shm`` of ``tmpfs_mb`` each."""
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--ro-bind', '/usr', '/usr']
    for path in HOST_ROOTS:
        if os.path.islink(path):
            command += ['--symlink', os.readlink(path), path]
        elif os.path.isdir(path):
            command += ['--ro-bind', path, path]
    for path in HOST_FILES:
        command += ['--ro-bind-try', path, path]
    size = str(tmpfs_mb << 20)
    command += [
        '--proc',
        '/proc',
        '--dev',
        '/dev',
        '--size',
        size,
        '--tmpfs',
        '/tmp',
        '--size',
        size,
        '--tmpfs',
        '/dev/shm',
    ]
    for path in python_runtime():
        command += ['--ro-bind', path, path]
    return command


@functools.cache
def sandbox_error() -> str:
    """'' when bwrap runs here, else why not."""
    if shutil.which('bwrap') is None:
        return 'bwrap is not installed'
    try:
        proc = subprocess.run([*sandbox(64), '--', '/bin/true'], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if proc.returncode:
        return proc.stderr.decode(errors='replace').strip()[:REASON_CHARS] or f'exit {proc.returncode}'
    return ''


def solver_cpus() -> list[int]:
    return sorted(os.sched_getaffinity(0))[:SOLVER_CPUS]


def run_solver(box: Path, log: Path, time_limit_s: float, memory_mb: int) -> str:
    """Run ``box/solver/solve`` on ``box/instance`` into ``box/output``: '' when it exited 0 within the limit, else why
    not."""
    mounts = ['--ro-bind', box / 'instance', '/instance', '--bind', box / 'output', '/output']
    argv = [f'/work/{SOLVE}', '/instance', '/output']
    return run_sandboxed(box / 'solver', mounts, argv, log, time_limit_s, memory_mb, memory_mb)


def build(solver_dir: Path) -> str:
    """Run ``solver_dir/build`` in place, if there is one: '' when there is none, no sandbox (every seed then says so)
    or it exited 0 within ``BUILD_TIME_LIMIT_S`` leaving at most ``BUILD_OUTPUT_BYTES``, else why not."""
    if not (solver_dir / BUILD).is_file() or sandbox_error():
        return ''
    with tempfile.TemporaryDirectory(prefix='gt-log-') as private:
        log, limits = Path(private, 'build.log'), (BUILD_TIME_LIMIT_S, BUILD_MEMORY_MB, BUILD_TMPFS_MB)
        if failure := run_sandboxed(solver_dir, [], [f'/work/{BUILD}'], log, *limits):
            return failure
    try:
        size = sum(st.st_size for _, st in entries(solver_dir))
    except OSError as e:
        return f'build output unreadable: {e}'
    return f'build output over {BUILD_OUTPUT_BYTES} bytes' if size > BUILD_OUTPUT_BYTES else ''


def run_sandboxed(
    work: Path, mounts: list, argv: list[str], log: Path, time_limit_s: float, memory_mb: int, tmpfs_mb: int
) -> str:
    """Run ``argv`` in the sandbox with ``work`` as ``/work`` (the cwd) plus ``mounts``, output to ``log``: '' when it
    exited 0 within the limit, else why not."""
    memory, cpus = memory_mb << 20, solver_cpus()

    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_BYTES, FSIZE_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.sched_setaffinity(0, cpus)

    mounts = [*mounts, '--bind', work, '/work', '--chdir', '/work', '--remount-ro', '/']
    command = [*sandbox(tmpfs_mb), *map(str, mounts), '--', *argv]
    with log.open('wb') as out:
        proc = subprocess.Popen(
            command, env=ENV, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, preexec_fn=limit
        )
        try:
            code = proc.wait(timeout=time_limit_s)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # bwrap's init dies with it, taking the whole pid namespace
            except ProcessLookupError:
                pass
            proc.wait()
    if code is None:
        return f'timed out after {time_limit_s:g} s'
    if code != 0:
        tail = log.read_bytes()[-LOG_TAIL_CHARS:].decode(errors='replace').strip()
        return f'exit {code}: {tail}' if tail else f'exit {code}'
    return ''


def entries(top: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Every entry under ``top``, lstat'd. The evaluator owns the tree, so each directory gets u+rwx back before it is
    read (a solver cannot hide an entry behind ``chmod 111``); anything still unreadable raises ``OSError``."""

    def fail(error: OSError):
        raise error

    os.chmod(top, 0o700)
    for root, dirs, files in os.walk(top, onerror=fail):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(path, 0o700)
            yield path, st


def output_link(output_dir: Path) -> str:
    """The first entry that is a symlink, a hard link or not a regular file or directory; '' when there is none."""
    for path, st in entries(output_dir):
        if not (stat.S_ISDIR(st.st_mode) or (stat.S_ISREG(st.st_mode) and st.st_nlink == 1)):
            return os.path.basename(path)
    return ''


def run_seed(challenge: ModuleType, tier: str, seed: bytes, solver_dirs: Sequence[Path]) -> list[SeedResult]:
    """Generate the instance once, privately; then each solver in turn on its own copy of it, same limits."""
    if error := sandbox_error():
        return [SeedResult(False, 0.0, 0.0, f'sandbox unavailable: {error}'[:REASON_CHARS])] * len(solver_dirs)
    with ExitStack() as stack:
        instance_dir, secret_dir = (
            Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=p, ignore_cleanup_errors=True)))
            for p in ('gt-instance-', 'gt-secret-')
        )
        challenge.generate(seed, tier, instance_dir, secret_dir)
        return [solve(challenge, tier, solver_dir, instance_dir, secret_dir) for solver_dir in solver_dirs]


def solve(challenge: ModuleType, tier: str, solver_dir: Path, instance_dir: Path, secret_dir: Path) -> SeedResult:
    spec = challenge.TIERS[tier]
    seconds = 0.0

    def zero(reason: str) -> SeedResult:
        return SeedResult(False, 0.0, seconds, reason[:REASON_CHARS])

    with ExitStack() as stack:
        box, private = (
            Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=p, ignore_cleanup_errors=True)))
            for p in ('gt-solve-', 'gt-log-')
        )
        (box / 'output').mkdir()
        try:
            shutil.copytree(instance_dir, box / 'instance')
            shutil.copytree(solver_dir, box / 'solver', symlinks=True)
        except (OSError, shutil.Error) as e:
            return zero(f'cannot copy the solver: {e}')
        started = time.monotonic()
        failure = run_solver(box, private / 'solve.log', spec.time_limit_s, spec.memory_mb)
        seconds = round(time.monotonic() - started, 3)
        if not failure:
            try:
                if link := output_link(box / 'output'):
                    failure = f'output holds a link or special file: {link}'
            except OSError as e:
                failure = f'output unreadable: {e}'
        if failure:
            return zero(failure)
        try:
            verdict = challenge.check(instance_dir, secret_dir, box / 'output')
            valid, score, reason = bool(verdict.valid), float(verdict.score), str(verdict.reason)[:REASON_CHARS]
        except Exception as e:  # check must never raise; if it does, the seed scores 0 rather than the run failing
            return zero(f'check raised {type(e).__name__}: {e}')
    if not valid or not math.isfinite(score) or score < 0:
        return zero(reason or f'score {score}')
    return SeedResult(True, score, seconds, reason)


def evaluate(
    challenge: ModuleType, tier: str, seed_block_hash: str, n: int, solver_dirs: Sequence[Path]
) -> list[list[SeedResult]]:
    """Per solver, its result on each of the n seeds. Each solver's ``build`` runs first, once, in its dir; a failed
    build scores every seed 0. Odd seeds run the solvers in reverse, so none always goes first."""
    failures = [build(solver_dir) for solver_dir in solver_dirs]
    ready = [solver_dir for solver_dir, failure in zip(solver_dirs, failures) if not failure]
    rows = []
    for i, seed in enumerate(derive_seeds(seed_block_hash, n) if ready else []):
        step = -1 if i % 2 else 1
        rows.append(run_seed(challenge, tier, seed, ready[::step])[::step])
    built = zip(*rows)
    return [
        [SeedResult(False, 0.0, 0.0, f'build failed: {failure}'[:REASON_CHARS])] * n if failure else list(next(built))
        for failure in failures
    ]
