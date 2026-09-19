# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What the gateway enforces on an OpenAI body, and nothing more (vault ``25`` "Front door types").

Limits only: ``n == 1`` and ``best_of == 1`` (one request, one completion: the lease accounting check compares the
tokens of the one answer returned with the runtime's own counters), remote media, and that a token field the client
names is a positive integer. The gateway keeps **no output cap of its own** (Kimbo 9/16): ``max_tokens`` /
``max_completion_tokens`` are forwarded exactly as sent and nothing is injected when the client names none; the
runtime's own limit applies (the sparkinfer container's ``SPARKINFER_MAX_OUTPUT_TOKENS``, 16384 by default), and das
forwards the fields as sent too. The body is never rebuilt and message shapes are never checked: ``tools``,
``tool_choice``, ``parallel_tool_calls``, assistant ``tool_calls`` history, ``role: tool`` and content parts all reach
the runtime as sent. Phase 0's string-only message check is what silently dropped ``tools`` (Spark-Hermes report, 9/14);
it does not come with the gateway. A request a model cannot serve is the runtime's 400 to return.
"""

from __future__ import annotations

import json
from typing import Any

_TOKEN_FIELDS = ('max_tokens', 'max_completion_tokens')
_SINGLE_FIELDS = ('n', 'best_of')
_MEDIA_PARTS = ('image_url', 'video_url')


class RequestRefused(Exception):
    """Refused at the gateway before any instance sees it."""

    def __init__(self, status: int, message: str, error_type: str = 'invalid_request_error'):
        super().__init__(message)
        self.status, self.message, self.error_type = status, message, error_type

    def body(self) -> dict[str, Any]:
        return {'error': {'type': self.error_type, 'message': self.message}}


def parse_object(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw)
    except ValueError as e:
        raise RequestRefused(400, f'body is not JSON: {e}') from e
    if not isinstance(body, dict):
        raise RequestRefused(400, 'body must be a JSON object')
    return body


def enforce_openai_limits(body: dict[str, Any], path: str) -> None:
    """Refuse what breaks a limit. The body is never changed: the token fields go on as sent, or stay absent."""
    for key in _SINGLE_FIELDS:
        value = body.get(key)
        if value is not None and (isinstance(value, bool) or value != 1):
            raise RequestRefused(400, f'{key} must be 1')
    for key in _TOKEN_FIELDS:
        value = body.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise RequestRefused(400, f'{key} must be a positive integer')
    if path == '/v1/chat/completions':
        kind = remote_media(body.get('messages'))
        if kind:
            raise RequestRefused(400, f'remote media not supported yet: send {kind} as an inline data: URL')


def remote_media(messages: Any) -> str:
    """The first ``image_url`` / ``video_url`` content part that is not an inline ``data:`` URL, or ''. Workloads run
    with no egress, so a runtime cannot fetch one; downloading and inlining at the gateway comes later."""
    if not isinstance(messages, list):
        return ''
    for message in messages:
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            for kind in _MEDIA_PARTS:
                if kind not in part:
                    continue
                ref = part[kind]
                url = ref.get('url') if isinstance(ref, dict) else ref
                if isinstance(url, str) and not url.lstrip().lower().startswith('data:'):
                    return kind
    return ''
