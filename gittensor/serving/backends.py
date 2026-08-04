# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Inference backends for serving miners (sub-subnet B beta).

The serving miner talks to whatever produces tokens through one narrow
interface so the mock and the real runtime are interchangeable:

- ``EchoBackend`` — deterministic, GPU-free. Both miner and validator can
  compute ``expected_completion`` locally, which makes golden-output
  verification work on localnet with zero inference infrastructure.
- ``OpenAICompatBackend`` — posts to any OpenAI-style ``/v1/completions``
  server (sparkinfer serving mode, vLLM, llama.cpp server) with greedy
  decode. This is the seam the real runtime plugs into.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from gittensor.serving.loadout import ServingLoadout

ECHO_BACKEND = 'echo'
OPENAI_COMPAT_BACKEND = 'openai-compat'


@dataclass
class GenerationResult:
    completion: str
    model_id: str
    generation_ms: float


class InferenceBackend(Protocol):
    model_id: str

    def generate(self, prompt: str, max_tokens: int) -> GenerationResult: ...


def expected_completion(prompt: str, max_tokens: int, model_id: str) -> str:
    """Deterministic pseudo-completion both miner and validator can derive.

    Stands in for greedy decode on pinned weights: same inputs, same string,
    every time, on any machine. One hex 'token' per max_tokens unit.
    """
    tokens = []
    seed = f'{model_id}:{prompt}'.encode()
    digest = hashlib.sha256(seed).hexdigest()
    for i in range(max_tokens):
        digest = hashlib.sha256(f'{digest}:{i}'.encode()).hexdigest()
        tokens.append(digest[:8])
    return ' '.join(tokens)


class EchoBackend:
    """GPU-free deterministic backend for localnet/testnet bring-up."""

    def __init__(self, loadout: ServingLoadout):
        self.model_id = loadout.model_id

    def generate(self, prompt: str, max_tokens: int) -> GenerationResult:
        start = time.monotonic()
        completion = expected_completion(prompt, max_tokens, self.model_id)
        return GenerationResult(
            completion=completion,
            model_id=self.model_id,
            generation_ms=(time.monotonic() - start) * 1000.0,
        )


class OpenAICompatBackend:
    """Backend for a local OpenAI-compatible inference server (sparkinfer/vLLM).

    Greedy decode (temperature 0) so outputs are candidates for golden-output
    verification once the runtime commits to deterministic decode.
    """

    def __init__(self, loadout: ServingLoadout):
        if not loadout.base_url:
            raise ValueError('openai-compat backend requires base_url in the serving loadout')
        self.model_id = loadout.model_id
        self.base_url = loadout.base_url.rstrip('/')

    def generate(self, prompt: str, max_tokens: int) -> GenerationResult:
        start = time.monotonic()
        response = requests.post(
            f'{self.base_url}/v1/completions',
            json={
                'model': self.model_id,
                'prompt': prompt,
                'max_tokens': max_tokens,
                'temperature': 0,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return GenerationResult(
            completion=payload['choices'][0]['text'],
            model_id=payload.get('model', self.model_id),
            generation_ms=(time.monotonic() - start) * 1000.0,
        )


def load_backend(loadout: ServingLoadout) -> InferenceBackend:
    if loadout.backend == ECHO_BACKEND:
        return EchoBackend(loadout)
    if loadout.backend == OPENAI_COMPAT_BACKEND:
        return OpenAICompatBackend(loadout)
    raise ValueError(f'Unknown serving backend: {loadout.backend}')
