#!/usr/bin/env python3
"""Attestation sidecar for the serving runtime image: runs gt_attest on request (stdlib only).

POST /v1/attest  {"seed": <u64>, "iters": <n>, "fill": true}  -> gt_attest's JSON plus "queued_ms"
GET  /v1/attest/info                                          -> devices without running a challenge (iters 1, no fill,
                                                                 small working set)
Challenges run one at a time; concurrent requests queue and report their own wall including the wait, which is what
lets a validator see two hotkeys sharing one card. Optional bearer via ATTEST_API_KEY.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.environ.get('GT_ATTEST_BIN', '/opt/sparkinfer/bin/gt_attest')
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
        if self.path != '/v1/attest/info' or not self._auth():
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
