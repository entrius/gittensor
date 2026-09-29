# The MIT License (MIT)
# Copyright © 2026 Entrius

"""Run one solver on N fresh instances and score it: per seed, ``generate`` -> ``./solve <instance> <output>`` ->
``check``.

Seeds come from a public seed string (a future block hash): ``sha256(f'{public_seed}:{i}')``, so anyone can replay a
score. ``generate`` writes into private dirs (instance and secret, each its own ``mkdtemp``); the solver gets copies
in a third: the instance (read-only), an empty output dir, and its own directory (a fresh copy per seed, so no state
carries). ``check`` reads the private originals plus the output, and output holding a link or a special file scores 0.

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

Same-uid namespaces are defense in depth, not the trust anchor: until the attested evaluator image (Polaris) runs this
runner, every registry ``emission_share`` stays 0.
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
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

SOLVE = 'solve'
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
    seed: str  # hex
    valid: bool
    score: float
    seconds: float
    reason: str = ''


@dataclass(frozen=True)
class Evaluation:
    challenge_id: str
    version: str
    tier: str
    public_seed: str
    cpus: int
    results: list[SeedResult]

    @property
    def score(self) -> float:
        return sum(r.score for r in self.results) / len(self.results) if self.results else 0.0


def derive_seeds(public_seed: str, n: int) -> list[bytes]:
    return [hashlib.sha256(f'{public_seed}:{i}'.encode()).digest() for i in range(n)]


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


def sandbox(memory_mb: int) -> list[str]:
    """The bwrap command up to the solver's own mounts."""
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--ro-bind', '/usr', '/usr']
    for path in HOST_ROOTS:
        if os.path.islink(path):
            command += ['--symlink', os.readlink(path), path]
        elif os.path.isdir(path):
            command += ['--ro-bind', path, path]
    for path in HOST_FILES:
        command += ['--ro-bind-try', path, path]
    size = str(memory_mb << 20)
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
    memory, cpus = memory_mb << 20, solver_cpus()

    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_BYTES, FSIZE_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.sched_setaffinity(0, cpus)

    mounts = ['--ro-bind', box / 'instance', '/instance', '--bind', box / 'output', '/output']
    mounts += ['--bind', box / 'solver', '/work', '--chdir', '/work', '--remount-ro', '/']
    command = [*sandbox(memory_mb), *map(str, mounts), '--', f'/work/{SOLVE}', '/instance', '/output']
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


def output_link(output_dir: Path) -> str:
    """The first entry that is a symlink, a hard link or not a regular file or directory; '' when there is none. The
    evaluator owns the tree, so each directory gets u+rwx back before it is read (a solver cannot hide an entry behind
    ``chmod 111``); anything still unreadable raises ``OSError``."""

    def fail(error: OSError):
        raise error

    os.chmod(output_dir, 0o700)
    for root, dirs, files in os.walk(output_dir, onerror=fail):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(path, 0o700)
            elif not (stat.S_ISREG(st.st_mode) and st.st_nlink == 1):
                return name
    return ''


def run_seed(challenge: ModuleType, solver_dir: Path, tier: str, seed: bytes) -> SeedResult:
    spec = challenge.TIERS[tier]
    seconds = 0.0

    def zero(reason: str) -> SeedResult:
        return SeedResult(seed.hex(), False, 0.0, seconds, reason[:REASON_CHARS])

    if error := sandbox_error():
        return zero(f'sandbox unavailable: {error}')
    with ExitStack() as stack:
        instance_dir, secret_dir, box, private = (
            Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=p, ignore_cleanup_errors=True)))
            for p in ('gt-instance-', 'gt-secret-', 'gt-solve-', 'gt-log-')
        )
        challenge.generate(seed, tier, instance_dir, secret_dir)
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
    return SeedResult(seed.hex(), True, score, seconds, reason)


def evaluate(challenge: ModuleType, solver_dir: str | Path, tier: str, public_seed: str, seeds: int) -> Evaluation:
    solver_dir = Path(solver_dir).resolve()
    results = [run_seed(challenge, solver_dir, tier, seed) for seed in derive_seeds(public_seed, seeds)]
    return Evaluation(challenge.CHALLENGE_ID, challenge.VERSION, tier, public_seed, len(solver_cpus()), results)
