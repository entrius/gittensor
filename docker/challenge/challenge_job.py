#!/usr/bin/env python3
"""The one-shot GPU proof (image gt-challenge, docker/challenge/Dockerfile): run gt_challenge once, print JSON, exit.

    challenge_job.py --seed <u64> [--fill-ratio 0.9] [--iters 3] [--device 0] [--dim 1024] [--matrices 512]

The controller runs it per pinned card over SSH as `docker run --rm --gpus device=<uuid> <image> --seed ...`, and
judges the printed {digest, wall_ms, filled_bytes, uuid, ...} against the challenge bank
(gittensor/controller/challenge/bank.py). Stdlib only; copied from docker/attest/attest_server.py's `run` with the
HTTP server and the queue removed — there is nothing to queue, the job runs on an empty card and exits. Exit 0 with
the job's JSON on success; exit 1 with {"error", "exit"} when the binary fails or times out.
"""

import argparse
import json
import os
import subprocess
import sys
import time

BIN = os.environ.get('GT_CHALLENGE_BIN', '/opt/gt-challenge/bin/gt_challenge')
VERSION = os.environ.get('GT_CHALLENGE_VERSION', 'dev')


def build_args(seed: int, fill_ratio: float, iters: int, device: int, dim: int, matrices: int) -> list:
    if not 0.0 <= fill_ratio <= 1.0:
        raise ValueError('fill_ratio must be within 0..1')
    return [
        '--seed',
        str(int(seed)),
        '--fill-ratio',
        repr(float(fill_ratio)),
        '--iters',
        str(max(1, min(int(iters), 20))),
        '--device',
        str(int(device)),
        '--dim',
        str(int(dim)),
        '--matrices',
        str(int(matrices)),
    ]


def run(args: list, timeout: float, binary: str = '') -> tuple:
    """(exit code for this job, payload) — the binary's JSON with `job_version` and `run_ms` added, or an error."""
    binary = binary or BIN
    started = time.monotonic()
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, {'error': f'timeout after {timeout:.0f} s', 'exit': None, 'job_version': VERSION}
    except OSError as e:
        return 1, {'error': f'cannot run {binary}: {e}', 'exit': None, 'job_version': VERSION}
    run_ms = round((time.monotonic() - started) * 1000.0, 1)
    if proc.returncode != 0:
        return 1, {
            'error': (proc.stdout or proc.stderr).strip()[:500],
            'exit': proc.returncode,
            'run_ms': run_ms,
            'job_version': VERSION,
        }
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return 1, {'error': f'no JSON from binary: {proc.stdout.strip()[:200]}', 'exit': 0, 'job_version': VERSION}
    if out.get('error'):
        return 1, {**out, 'job_version': VERSION}
    out['job_version'] = VERSION
    out['run_ms'] = run_ms
    return 0, out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Gittensor GPU proof: one seeded fill + GEMM chain on one card.')
    p.add_argument('--seed', type=int, required=True)
    p.add_argument('--fill-ratio', type=float, default=0.9)
    p.add_argument('--iters', type=int, default=3)
    p.add_argument('--device', type=int, default=0)
    p.add_argument('--dim', type=int, default=1024)
    p.add_argument('--matrices', type=int, default=512)
    p.add_argument('--timeout', type=float, default=150.0)
    a = p.parse_args(argv)
    try:
        args = build_args(a.seed, a.fill_ratio, a.iters, a.device, a.dim, a.matrices)
    except ValueError as e:
        print(json.dumps({'error': str(e), 'exit': None}))
        return 2
    code, payload = run(args, a.timeout)
    print(json.dumps(payload))
    return code


if __name__ == '__main__':
    sys.exit(main())
