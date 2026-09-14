# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The challenge bank (``23`` §3): the GPU proof's expected answers, precomputed on our own cards.

The one-shot job in ``docker/challenge`` fills an empty card and runs the deterministic GEMM chain; for a given
``(seed, iters, dim, matrices)`` every sm_120 card produces the same digest, and a real 5090 does it in about the
same wall time. So instead of a live reference sidecar, a **generator** runs the job for N seeds on a card we own
and writes a bank file, and a **consumer** hands each seed out exactly once, then judges the box's ``{digest,
wall_ms, filled_bytes, uuid}`` against the stored entry with a wall-time budget ratio. A seed is spent the moment it
is checked out (persisted before the box sees it), so a crash never replays one. The consumer reports how many
seeds remain and when the bank is running low or is empty.

    python -m gittensor.controller.challenge.bank generate --out bank.json --seeds 100 --device <GPU-UUID>
    python -m gittensor.controller.challenge.bank status --bank bank.json --used bank.used.json
"""

import argparse
import json
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

from gittensor.controller.checks import config as cfg

BANK_VERSION = 1


class BankDepleted(RuntimeError):
    """Every seed in the bank has been handed out."""


@dataclass(frozen=True)
class ChallengeParams:
    """The job parameters an answer must have been produced with. The digest depends on iters/dim/matrices; the wall
    time depends on all four, so an answer with other parameters is not comparable to the bank."""

    iters: int = cfg.CHALLENGE_ITERS
    fill_ratio: float = cfg.CHALLENGE_FILL_RATIO
    dim: int = cfg.CHALLENGE_DIM
    matrices: int = cfg.CHALLENGE_MATRICES

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ChallengeParams':
        return cls(int(d['iters']), float(d['fill_ratio']), int(d['dim']), int(d['matrices']))

    def job_args(self, seed: int) -> List[str]:
        return [
            '--seed',
            str(seed),
            '--fill-ratio',
            repr(float(self.fill_ratio)),
            '--iters',
            str(self.iters),
            '--dim',
            str(self.dim),
            '--matrices',
            str(self.matrices),
        ]

    def matches(self, answer: dict) -> bool:
        try:
            return (
                int(answer.get('iters', self.iters)) == self.iters
                and int(answer.get('dim', self.dim)) == self.dim
                and int(answer.get('matrices', self.matrices)) == self.matrices
                and abs(float(answer.get('fill_ratio', self.fill_ratio)) - self.fill_ratio) < 1e-6
            )
        except (TypeError, ValueError):
            return False


@dataclass
class BankEntry:
    seed: int
    digest: str
    wall_ms: float
    filled_bytes: int
    uuid: str = ''  # the card that produced it (telemetry)
    card_name: str = ''
    driver: str = ''
    image_digest: str = ''
    generated_at: float = 0.0
    run_ms: Optional[float] = None  # generator's outer clock around `docker run`, for the `D` timing question

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'BankEntry':
        return cls(
            seed=int(d['seed']),
            digest=str(d['digest']),
            wall_ms=float(d['wall_ms']),
            filled_bytes=int(d.get('filled_bytes') or 0),
            uuid=str(d.get('uuid') or ''),
            card_name=str(d.get('card_name') or ''),
            driver=str(d.get('driver') or ''),
            image_digest=str(d.get('image_digest') or ''),
            generated_at=float(d.get('generated_at') or 0.0),
            run_ms=None if d.get('run_ms') is None else float(d['run_ms']),
        )


@dataclass
class ChallengeBank:
    params: ChallengeParams
    entries: List[BankEntry]
    card_name: str = ''
    driver: str = ''
    image_digest: str = ''
    generated_at: float = 0.0
    version: int = BANK_VERSION

    def __post_init__(self):
        seeds = [e.seed for e in self.entries]
        if len(set(seeds)) != len(seeds):
            raise ValueError('challenge bank has duplicate seeds')

    @property
    def seeds(self) -> List[int]:
        return [e.seed for e in self.entries]

    def entry(self, seed: int) -> Optional[BankEntry]:
        return next((e for e in self.entries if e.seed == seed), None)

    def as_dict(self) -> dict:
        return {
            'version': self.version,
            'params': self.params.as_dict(),
            'card_name': self.card_name,
            'driver': self.driver,
            'image_digest': self.image_digest,
            'generated_at': self.generated_at,
            'entries': [e.as_dict() for e in self.entries],
        }

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.as_dict(), indent=1, sort_keys=True))

    @classmethod
    def from_dict(cls, d: dict) -> 'ChallengeBank':
        if int(d.get('version', 0)) != BANK_VERSION:
            raise ValueError(f'challenge bank version {d.get("version")} != {BANK_VERSION}')
        return cls(
            params=ChallengeParams.from_dict(d['params']),
            entries=[BankEntry.from_dict(e) for e in d.get('entries', [])],
            card_name=str(d.get('card_name') or ''),
            driver=str(d.get('driver') or ''),
            image_digest=str(d.get('image_digest') or ''),
            generated_at=float(d.get('generated_at') or 0.0),
        )

    @classmethod
    def load(cls, path) -> 'ChallengeBank':
        return cls.from_dict(json.loads(Path(path).read_text()))


# ---------------------------------------------------------------- consumer ----------------------------------------


class BankConsumer:
    """Hands out each seed once. ``used_path`` is a JSON list of spent seeds, rewritten on every checkout before the
    entry is returned; reopening the same pair resumes where it left off."""

    def __init__(self, bank: ChallengeBank, used_path, low_water: int = cfg.BANK_LOW_WATER):
        self.bank = bank
        self.used_path = Path(used_path)
        self.low_water = low_water
        self.used: Set[int] = set()
        if self.used_path.exists():
            self.used = {int(s) for s in json.loads(self.used_path.read_text() or '[]')}

    @classmethod
    def open(cls, bank_path, used_path=None, low_water: int = cfg.BANK_LOW_WATER) -> 'BankConsumer':
        bank_path = Path(bank_path)
        return cls(ChallengeBank.load(bank_path), used_path or bank_path.with_suffix('.used.json'), low_water)

    @property
    def remaining(self) -> int:
        return sum(1 for e in self.bank.entries if e.seed not in self.used)

    @property
    def depleted(self) -> bool:
        return self.remaining == 0

    @property
    def low(self) -> bool:
        return self.remaining <= self.low_water

    def status(self) -> dict:
        return {
            'total': len(self.bank.entries),
            'used': len(self.used),
            'remaining': self.remaining,
            'low': self.low,
            'depleted': self.depleted,
            'params': self.bank.params.as_dict(),
            'card_name': self.bank.card_name,
            'image_digest': self.bank.image_digest,
        }

    def checkout(self) -> BankEntry:
        """The next unused entry, marked used and persisted before it is returned. Raises ``BankDepleted``."""
        for entry in self.bank.entries:
            if entry.seed not in self.used:
                self.used.add(entry.seed)
                self._persist()
                return entry
        raise BankDepleted(f'challenge bank {self.used_path.stem}: all {len(self.bank.entries)} seeds used')

    def _persist(self) -> None:
        tmp = self.used_path.with_suffix(self.used_path.suffix + '.tmp')
        tmp.write_text(json.dumps(sorted(self.used)))
        tmp.replace(self.used_path)


@dataclass
class ProofVerdict:
    passed: bool
    reason: str
    uuid: str = ''
    wall_ms: Optional[float] = None
    filled_bytes: int = 0
    budget_ms: Optional[float] = None  # inner: the job's own wall vs the bank's wall
    elapsed_ms: Optional[float] = None
    outer_budget_ms: Optional[float] = None  # outer: our round trip vs the bank's run_ms + slack

    def as_dict(self) -> dict:
        return asdict(self)


def judge_answer(
    entry: BankEntry,
    answer: dict,
    params: ChallengeParams,
    budget_ratio: float = cfg.CHALLENGE_BUDGET_RATIO,
    min_fill_ratio: float = cfg.CHALLENGE_MIN_FILL_RATIO,
    expected_uuid: str = '',
    vram_total_bytes: Optional[int] = None,
    elapsed_ms: Optional[float] = None,
    rtt_slack_ms: float = cfg.CHALLENGE_RTT_SLACK_MS,
) -> ProofVerdict:
    """A box's job output against its bank entry. Passes when the digest matches, both clocks are within budget, at
    least ``min_fill_ratio`` of the requested fill was allocated, and the card that answered is the card we targeted.
    Two clocks, each against its own reference: the job's own ``wall_ms`` (fill + chain) vs ``budget_ratio`` x the
    bank's ``wall_ms``; and ``elapsed_ms`` — our clock around the whole SSH round trip + ``docker run`` — vs
    ``budget_ratio`` x the bank's ``run_ms`` (its outer clock; the bank's wall when it has none) plus ``rtt_slack_ms``.
    Mirrors ``gittensor/validator/serving/attest.py judge_card`` minus the model-resident test (this card is meant to be
    empty)."""
    try:
        return _judge_answer(
            entry,
            answer,
            params,
            budget_ratio,
            min_fill_ratio,
            expected_uuid,
            vram_total_bytes,
            elapsed_ms,
            rtt_slack_ms,
        )
    except (TypeError, ValueError, AttributeError) as e:
        return ProofVerdict(False, f'malformed answer: {e!r}'[:200])


def _judge_answer(
    entry, answer, params, budget_ratio, min_fill_ratio, expected_uuid, vram_total_bytes, elapsed_ms, rtt_slack_ms
):
    if not isinstance(answer, dict):
        return ProofVerdict(False, 'answer is not a JSON object')
    if answer.get('error'):
        return ProofVerdict(False, f'job error: {answer["error"]}'[:200])
    uuid = str(answer.get('uuid') or '')
    wall = float(answer.get('wall_ms') or 0.0)
    filled = int(answer.get('filled_bytes') or 0)
    budget = budget_ratio * entry.wall_ms if entry.wall_ms > 0 else float('inf')
    outer_ref = entry.run_ms if entry.run_ms else entry.wall_ms
    outer_budget = budget_ratio * outer_ref + rtt_slack_ms if outer_ref > 0 else float('inf')

    def fail(reason: str) -> ProofVerdict:
        return ProofVerdict(False, reason, uuid, wall, filled, budget, elapsed_ms, outer_budget)

    if int(answer.get('seed', entry.seed)) != entry.seed:
        return fail('answer is for a different seed')
    if not params.matches(answer):
        return fail('answer produced with other job parameters')
    if str(answer.get('digest') or '') != entry.digest:
        return fail('digest mismatch')
    if wall > budget:
        return fail(f'too slow: {wall:.0f} ms > {budget:.0f} ms budget')
    if elapsed_ms is not None and elapsed_ms > outer_budget:
        return fail(f'too slow: {elapsed_ms:.0f} ms round trip > {outer_budget:.0f} ms budget')
    total = float(vram_total_bytes or answer.get('vram_total') or 0.0)
    want = params.fill_ratio * total
    if want > 0 and filled < min_fill_ratio * want:
        return fail(f'under-filled: {filled / 1e9:.1f} GB of {want / 1e9:.1f} GB requested')
    if expected_uuid and uuid != expected_uuid:
        return fail(f'answered from {uuid or "?"}, not {expected_uuid}')
    return ProofVerdict(True, 'ok', uuid, wall, filled, budget, elapsed_ms, outer_budget)


# ---------------------------------------------------------------- generator ---------------------------------------


def image_ref(
    image: str = cfg.CHALLENGE_IMAGE, tag: str = cfg.CHALLENGE_IMAGE_TAG, digest: str = cfg.CHALLENGE_IMAGE_DIGEST
) -> str:
    """``image@sha256:...`` when a digest is pinned, else ``image:tag`` (dev only)."""
    return f'{image}@{digest}' if digest else f'{image}:{tag}'


def job_command(seed: int, params: ChallengeParams, image: str, device: str = '') -> List[str]:
    """The ``docker run`` that executes the one-shot job on one card. ``device`` is a GPU UUID (or index); inside the
    container that card is device 0, so the job is asked for device 0 and must answer with the same UUID."""
    gpus = f'"device={device}"' if device else 'all'
    return ['docker', 'run', '--rm', f'--gpus={gpus}', image, *params.job_args(seed), '--device', '0']


def job_command_str(seed: int, params: ChallengeParams, image: str, device: str = '') -> str:
    return ' '.join(
        shlex.quote(a) if not a.startswith('--gpus=') else a for a in job_command(seed, params, image, device)
    )


def run_local_job(
    seed: int, params: ChallengeParams, image: str, device: str = '', timeout: float = cfg.CHALLENGE_JOB_TIMEOUT_S
) -> dict:
    """Run the job on THIS machine's docker (the generator's card). Adds ``run_ms``, the outer clock."""
    started = time.monotonic()
    proc = subprocess.run(job_command(seed, params, image, device), capture_output=True, text=True, timeout=timeout)
    run_ms = (time.monotonic() - started) * 1000.0
    if proc.returncode != 0:
        return {'error': (proc.stdout or proc.stderr).strip()[:500], 'exit': proc.returncode, 'run_ms': run_ms}
    out = json.loads(proc.stdout)
    out['run_ms'] = round(run_ms, 1)
    return out


def random_seeds(n: int) -> List[int]:
    seeds: Set[int] = set()
    while len(seeds) < n:
        seeds.add(secrets.randbits(62))
    return sorted(seeds)


def generate_bank(
    run_job: Callable[[int], dict],
    seeds: Iterable[int],
    params: ChallengeParams,
    image_digest: str = '',
    now: Optional[float] = None,
    on_entry: Optional[Callable[[BankEntry], None]] = None,
) -> ChallengeBank:
    """Run ``run_job(seed)`` for every seed and collect the answers. An answer with an ``error`` is skipped (and
    reported), so a bank is only ever made of complete entries; the params in the bank are the params every entry
    was made with."""
    now = time.time() if now is None else now
    entries: List[BankEntry] = []
    card_names: Set[str] = set()
    drivers: Set[str] = set()
    for seed in seeds:
        answer = run_job(seed)
        if not isinstance(answer, dict) or answer.get('error') or not answer.get('digest'):
            print(f'seed {seed}: skipped ({(answer or {}).get("error", "no digest")})', file=sys.stderr)
            continue
        if not params.matches(answer):
            raise ValueError(f'seed {seed}: job answered with other parameters than {params}')
        entry = BankEntry(
            seed=seed,
            digest=str(answer['digest']),
            wall_ms=float(answer['wall_ms']),
            filled_bytes=int(answer.get('filled_bytes') or 0),
            uuid=str(answer.get('uuid') or ''),
            card_name=str(answer.get('name') or ''),
            driver=str(answer.get('driver') or ''),
            image_digest=image_digest,
            generated_at=now,
            run_ms=None if answer.get('run_ms') is None else float(answer['run_ms']),
        )
        entries.append(entry)
        card_names.add(entry.card_name)
        drivers.add(entry.driver)
        if on_entry:
            on_entry(entry)
    return ChallengeBank(
        params=params,
        entries=entries,
        card_name=card_names.pop() if len(card_names) == 1 else ','.join(sorted(card_names)),
        driver=drivers.pop() if len(drivers) == 1 else ','.join(sorted(drivers)),
        image_digest=image_digest,
        generated_at=now,
    )


def timing_summary(entries: Sequence[BankEntry]) -> Dict[str, Optional[float]]:
    """min / median / max of the job's own wall and of the outer docker-run clock — what question ``D`` in ``24``
    §4 needs from the check leg of the cycle."""

    def stats(values: List[float]) -> Dict[str, Optional[float]]:
        if not values:
            return {'min': None, 'median': None, 'max': None}
        s = sorted(values)
        return {'min': s[0], 'median': s[len(s) // 2], 'max': s[-1]}

    walls = stats([e.wall_ms for e in entries])
    runs = stats([e.run_ms for e in entries if e.run_ms is not None])
    return {f'wall_ms_{k}': v for k, v in walls.items()} | {f'run_ms_{k}': v for k, v in runs.items()}


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog='python -m gittensor.controller.challenge.bank', description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('generate', help='run the job for N seeds on a local card and write a bank file')
    g.add_argument('--out', required=True)
    g.add_argument('--seeds', type=int, default=100)
    g.add_argument('--device', default='', help='GPU UUID (or index) of the card to use; default all')
    g.add_argument('--image', default=image_ref())
    g.add_argument('--image-digest', default=cfg.CHALLENGE_IMAGE_DIGEST)
    g.add_argument('--fill-ratio', type=float, default=cfg.CHALLENGE_FILL_RATIO)
    g.add_argument('--iters', type=int, default=cfg.CHALLENGE_ITERS)
    g.add_argument('--timeout', type=float, default=cfg.CHALLENGE_JOB_TIMEOUT_S)
    s = sub.add_parser('status', help='how many seeds are left')
    s.add_argument('--bank', required=True)
    s.add_argument('--used', default=None)
    args = p.parse_args(argv)
    if args.cmd == 'status':
        print(json.dumps(BankConsumer.open(args.bank, args.used).status(), indent=1))
        return 0
    params = ChallengeParams(iters=args.iters, fill_ratio=args.fill_ratio)

    def progress(entry: BankEntry) -> None:
        print(
            f'seed {entry.seed} digest {entry.digest[:12]} wall {entry.wall_ms:.0f} ms '
            f'run {entry.run_ms or 0:.0f} ms fill {entry.filled_bytes / 1e9:.1f} GB {entry.uuid}',
            file=sys.stderr,
        )

    bank = generate_bank(
        lambda seed: run_local_job(seed, params, args.image, args.device, args.timeout),
        random_seeds(args.seeds),
        params,
        image_digest=args.image_digest,
        on_entry=progress,
    )
    bank.save(args.out)
    print(json.dumps({'entries': len(bank.entries), 'out': args.out, **timing_summary(bank.entries)}, indent=1))
    return 0 if bank.entries else 1


if __name__ == '__main__':
    sys.exit(main())
