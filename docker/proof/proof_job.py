#!/usr/bin/env python3
"""The GPU proof's launcher inside entrius/gt-proof (docker/proof/Dockerfile): run the STAGED binary once, print one
JSON object, exit.

    proof_job.py [--timeout 150] [-- <arguments for the binary>]

The image ships no binary. The controller copies the current sealed binary to $GT_PROOF_BIN (default
/opt/gt-proof/bin/gt_proof) and its challenge alongside before starting the container; whatever the binary prints on
stdout is passed through as the job's JSON with `job_version` and `run_ms` (our clock around the binary) added.
Stdlib only. Exit 0 with the JSON on success; exit 1 with {"error", "exit"} when nothing is staged, the binary fails,
prints no JSON, or times out.
"""

import argparse
import json
import os
import subprocess
import sys
import time

BIN = os.environ.get('GT_PROOF_BIN', '/opt/gt-proof/bin/gt_proof')
VERSION = os.environ.get('GT_PROOF_VERSION', 'dev')


def run(args: list, timeout: float, binary: str = '') -> tuple:
    """(exit code for this job, payload) — the binary's JSON with `job_version` and `run_ms` added, or an error."""
    binary = binary or BIN
    if not os.path.isfile(binary):
        return 1, {'error': f'nothing staged at {binary}', 'exit': None, 'job_version': VERSION}
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
    if not isinstance(out, dict):
        return 1, {'error': 'binary printed JSON that is not an object', 'exit': 0, 'job_version': VERSION}
    if out.get('error'):
        return 1, {**out, 'job_version': VERSION}
    out['job_version'] = VERSION
    out['run_ms'] = run_ms
    return 0, out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Gittensor GPU proof launcher: run the staged binary once on this card.')
    p.add_argument('--timeout', type=float, default=150.0)
    p.add_argument('args', nargs='*', help='passed to the binary unchanged (put them after --)')
    a = p.parse_args(argv)
    code, payload = run(a.args, a.timeout)
    print(json.dumps(payload))
    return code


if __name__ == '__main__':
    sys.exit(main())
