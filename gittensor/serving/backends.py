# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Inference backends for serving miners (sub-subnet B beta).

The serving miner talks to whatever produces tokens through one narrow,
OpenAI-chat-shaped interface so the mock and the real runtime are
interchangeable:

- ``EchoBackend`` — deterministic, GPU-free. Both miner and validator can
  compute ``expected_completion`` locally, which makes audit verification work
  on localnet with zero inference infrastructure.
- ``OpenAICompatBackend`` — posts to an OpenAI-compatible
  ``/v1/chat/completions`` server (sparkinfer_server is the blessed runtime)
  with greedy decode and, when asked, per-token logprobs. This is the seam the
  real runtime plugs into.
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

import requests

from gittensor.serving.loadout import ServingLoadout

ECHO_BACKEND = 'echo'
OPENAI_COMPAT_BACKEND = 'openai-compat'

Message = Dict[str, str]


@dataclass
class GenerationResult:
    completion: str
    model_id: str
    generation_ms: float
    tokens: Optional[List[str]] = None
    token_logprobs: Optional[List[float]] = None
    ttft_ms: Optional[float] = None
    decode_tps: Optional[float] = None
    finish_reason: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)


class InferenceBackend(Protocol):
    model_id: str

    def generate(self, messages: List[Message], max_tokens: int, logprobs: bool = False) -> GenerationResult: ...


def flatten_messages(messages: List[Message]) -> str:
    return '\n'.join(f'{m.get("role", "user")}: {m.get("content", "")}' for m in messages)


def expected_completion(messages: List[Message], max_tokens: int, model_id: str) -> GenerationResult:
    """Deterministic pseudo-completion both miner and validator can derive.

    Stands in for greedy decode on pinned weights: same inputs, same tokens and
    same (fake) logprobs, every time, on any machine.
    """
    tokens: List[str] = []
    logprobs: List[float] = []
    digest = hashlib.sha256(f'{model_id}:{flatten_messages(messages)}'.encode()).hexdigest()
    for i in range(max_tokens):
        digest = hashlib.sha256(f'{digest}:{i}'.encode()).hexdigest()
        tokens.append(digest[:8])
        logprobs.append(-(int(digest[8:12], 16) % 1000) / 1000.0)
    return GenerationResult(
        completion=' '.join(tokens),
        model_id=model_id,
        generation_ms=0.0,
        tokens=tokens,
        token_logprobs=logprobs,
        finish_reason='length',
        usage={
            'prompt_tokens': len(messages),
            'completion_tokens': max_tokens,
            'total_tokens': len(messages) + max_tokens,
        },
    )


class EchoBackend:
    """GPU-free deterministic backend for localnet/testnet bring-up."""

    def __init__(self, loadout: ServingLoadout):
        self.model_id = loadout.model_id

    def generate(self, messages: List[Message], max_tokens: int, logprobs: bool = False) -> GenerationResult:
        start = time.monotonic()
        result = expected_completion(messages, max_tokens, self.model_id)
        result.generation_ms = (time.monotonic() - start) * 1000.0
        if not logprobs:
            result.tokens = None
            result.token_logprobs = None
        return result


class OpenAICompatBackend:
    """Backend for a local OpenAI-compatible chat server (sparkinfer_server).

    Greedy decode (temperature 0). sparkinfer returns additive timing fields
    (``ttft_ms``, ``generation_ms``, ``decode_tps``) which are passed through
    when present; other servers simply omit them.
    """

    def __init__(self, loadout: ServingLoadout):
        if not loadout.base_url:
            raise ValueError('openai-compat backend requires base_url in the serving loadout')
        self.model_id = loadout.model_id
        self.base_url = loadout.base_url.rstrip('/')
        self.timeout = loadout.request_timeout

    def generate(self, messages: List[Message], max_tokens: int, logprobs: bool = False) -> GenerationResult:
        body: Dict = {
            'model': self.model_id,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': 0,
            'stream': False,
        }
        if logprobs:
            body['logprobs'] = True
            body['top_logprobs'] = 1
        start = time.monotonic()
        response = requests.post(f'{self.base_url}/v1/chat/completions', json=body, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        elapsed_ms = (time.monotonic() - start) * 1000.0

        choice = payload['choices'][0]
        tokens: Optional[List[str]] = None
        token_logprobs: Optional[List[float]] = None
        content_lp = (choice.get('logprobs') or {}).get('content')
        if content_lp:
            tokens = [entry['token'] for entry in content_lp]
            token_logprobs = [float(entry['logprob']) for entry in content_lp]

        return GenerationResult(
            completion=choice['message'].get('content') or '',
            model_id=payload.get('model', self.model_id),
            generation_ms=float(payload.get('generation_ms', elapsed_ms)),
            tokens=tokens,
            token_logprobs=token_logprobs,
            ttft_ms=payload.get('ttft_ms'),
            decode_tps=payload.get('decode_tps'),
            finish_reason=choice.get('finish_reason'),
            usage=payload.get('usage') or {},
        )


def load_backend(loadout: ServingLoadout) -> InferenceBackend:
    if loadout.backend == ECHO_BACKEND:
        return EchoBackend(loadout)
    if loadout.backend == OPENAI_COMPAT_BACKEND:
        return OpenAICompatBackend(loadout)
    raise ValueError(f'Unknown serving backend: {loadout.backend}')
