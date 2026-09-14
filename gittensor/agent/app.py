# The MIT License (MIT)
# Copyright © 2025 Entrius

"""HTTP front for :class:`~gittensor.agent.service.AgentService` (stdlib ``http.server``, like ``docker/attest``).

Exactly three routes; anything else is 404. Bodies are JSON, capped at :data:`MAX_BODY_BYTES`.

    GET    /info              self-report (agent version, image digest, GPUs, sshd port)
    POST   /install_ssh_key   {pubkey, hotkey_ss58, nonce, timestamp, signature} -> append to root's authorized_keys
    DELETE /install_ssh_key   same body, signed for the remove action -> take it back out
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from gittensor.agent.service import AgentService

MAX_BODY_BYTES = 16 * 1024
KEY_ROUTE = '/install_ssh_key'
INFO_ROUTE = '/info'

logger = logging.getLogger('gt-agent.http')


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: AgentService):
        super().__init__(address, AgentHandler)
        self.service = service


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentServer
    server_version = 'gt-agent'
    sys_version = ''

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        """Decoded body, or None after a 4xx has been sent."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._send(400, {'error': 'bad Content-Length'})
            return None
        if length > MAX_BODY_BYTES:
            self._send(413, {'error': f'body over {MAX_BODY_BYTES} bytes'})
            return None
        raw = self.rfile.read(length) if length else b''
        try:
            payload = json.loads(raw or b'{}')
        except ValueError:
            self._send(400, {'error': 'body is not JSON'})
            return None
        if not isinstance(payload, dict):
            self._send(400, {'error': 'body must be a JSON object'})
            return None
        return payload

    def _path(self) -> str:
        return self.path.split('?', 1)[0].rstrip('/') or '/'

    def do_GET(self) -> None:
        if self._path() != INFO_ROUTE:
            return self._send(404, {'error': 'not found'})
        self._send(*self.server.service.info())

    def do_POST(self) -> None:
        if self._path() != KEY_ROUTE:
            return self._send(404, {'error': 'not found'})
        payload = self._read_json()
        if payload is not None:
            self._send(*self.server.service.install_ssh_key(payload))

    def do_DELETE(self) -> None:
        if self._path() != KEY_ROUTE:
            return self._send(404, {'error': 'not found'})
        payload = self._read_json()
        if payload is not None:
            self._send(*self.server.service.remove_ssh_key(payload))

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug('%s ' + fmt, self.client_address[0], *args)


def make_server(service: AgentService, host: str = '0.0.0.0', port: int = 0) -> AgentServer:
    """Bind (port 0 = ephemeral, for tests) without serving; call ``serve_forever`` on the result."""
    return AgentServer((host, port), service)
