#!/usr/bin/env python3
"""Attestation sidecar (image entrius/gt-attest, docker/attest/Dockerfile): runs gt_attest on request (stdlib only).

POST /v1/attest  {"seed": <u64>, "iters": <n>, "fill": true}  -> gt_attest's JSON plus "queued_ms"
GET  /v1/attest/info                                          -> devices without running a challenge (iters 1, no fill,
                                                                 small working set)
GET  /info                                                    -> self-report: GPUs as nvidia-smi sees them, driver,
                                                                 host CPU/RAM, sidecar version. Telemetry for the
                                                                 operator and the /compute page — a host can say
                                                                 anything here, so nothing is paid on it; the
                                                                 challenge above is the proof.
Challenges run one at a time; concurrent requests queue and report their own wall including the wait, which is what
lets a validator see two hotkeys sharing one card. Optional bearer via ATTEST_API_KEY.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.environ.get('GT_ATTEST_BIN', '/opt/gt-attest/bin/gt_attest')
VERSION = os.environ.get('GT_ATTEST_VERSION', 'dev')
PORT = int(os.environ.get('ATTEST_PORT', '8081'))
API_KEY = os.environ.get('ATTEST_API_KEY')
LOCK = threading.Lock()


def run(args, timeout=120.0):
    started = time.monotonic()
    with LOCK:
        queued_ms = (time.monotonic() - started) * 1000.0
        proc = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        return 500, {
            'error': (proc.stdout or proc.stderr).strip()[:500],
            'exit': proc.returncode,
            'queued_ms': queued_ms,
        }
    out = json.loads(proc.stdout)
    out['queued_ms'] = round(queued_ms, 1)
    return 200, out


def self_report() -> dict:
    query = 'uuid,name,driver_version,memory.total,memory.used,pci.bus_id,temperature.gpu,utilization.gpu'
    gpus, error = [], None
    try:
        out = subprocess.run(
            ['nvidia-smi', f'--query-gpu={query}', '--format=csv,noheader,nounits'],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if out.returncode != 0:
            error = (out.stderr or out.stdout).strip()[:300]
        for line in out.stdout.splitlines():
            cols = [c.strip() for c in line.split(',')]
            if len(cols) == 8:
                gpus.append(
                    {
                        'uuid': cols[0],
                        'name': cols[1],
                        'driver': cols[2],
                        'memory_total_mib': int(cols[3]),
                        'memory_used_mib': int(cols[4]),
                        'pci_bus_id': cols[5],
                        'temperature_c': int(cols[6]),
                        'utilization_pct': int(cols[7]),
                    }
                )
    except Exception as e:  # no nvidia-smi in the container, or a hung driver
        error = repr(e)[:300]
    mem_total_kib = None
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total_kib = int(line.split()[1])
                    break
    except OSError:
        pass
    return {
        'version': VERSION,
        'hostname': os.uname().nodename,
        'cpus': os.cpu_count(),
        'mem_total_gib': round(mem_total_kib / 1048576, 1) if mem_total_kib else None,
        'gpus': gpus,
        'error': error,
        'ts': time.time(),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if API_KEY and self.headers.get('Authorization') != f'Bearer {API_KEY}':
            self._send(401, {'error': 'unauthorized'})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        if self.path == '/info':
            return self._send(200, self_report())
        if self.path != '/v1/attest/info':
            return self._send(404, {'error': 'not found'})
        self._send(
            *run(['--seed', '1', '--iters', '1', '--dim', '256', '--matrices', '2', '--device', 'all'], timeout=30.0)
        )

    def do_POST(self):
        if self.path != '/v1/attest' or not self._auth():
            return self._send(404, {'error': 'not found'})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')) or b'{}'))
            seed, iters = int(body['seed']), int(body.get('iters', 3))
        except (ValueError, KeyError, TypeError):
            return self._send(400, {'error': 'seed (u64) required; iters optional'})
        args = ['--seed', str(seed), '--iters', str(max(1, min(iters, 20))), '--device', 'all']
        if body.get('fill', True):
            args.append('--fill')
        self._send(*run(args))

    def log_message(self, fmt, *args):  # quiet
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
