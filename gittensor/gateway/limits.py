# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What the gateway enforces on an OpenAI body, and nothing more (vault ``25`` "Front door types").

Limits only: the ``max_tokens`` cap, ``n == 1``, and remote media. The body is never rebuilt and message shapes are
never checked: ``tools``, ``tool_choice``, ``parallel_tool_calls``, assistant ``tool_calls`` history, ``role: tool``
and content parts all reach the runtime as sent. Phase 0's string-only message check is what silently dropped
``tools`` (Spark-Hermes report, 9/14); it does not come with the gateway. A request a model cannot serve is the
runtime's 400 to return.
"""

from __future__ import annotations

import json
from typing import Any

# Copied from gittensor.constants.SERVING_MAX_TOKENS, not imported: the gateway outlives phase 0.
MAX_TOKENS = 4096
_TOKEN_FIELDS = ('max_tokens', 'max_completion_tokens')
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


def enforce_openai_limits(body: dict[str, Any], path: str, cap: int = MAX_TOKENS) -> list[str]:
    """Refuse what breaks a limit; clamp the token fields in place. Returns the fields changed (empty: forward the
    original bytes)."""
    n = body.get('n')
    if n is not None and (isinstance(n, bool) or n != 1):
        raise RequestRefused(400, 'n must be 1')
    changed: list[str] = []
    present = [key for key in _TOKEN_FIELDS if body.get(key) is not None]
    for key in present:
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RequestRefused(400, f'{key} must be a positive integer')
        if value > cap:
            body[key] = cap
            changed.append(key)
    if not present:  # the cap holds for a request that names no limit too
        body['max_tokens'] = cap
        changed.append('max_tokens')
    if path == '/v1/chat/completions':
        kind = remote_media(body.get('messages'))
        if kind:
            raise RequestRefused(400, f'remote media not supported yet: send {kind} as an inline data: URL')
    return changed


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
