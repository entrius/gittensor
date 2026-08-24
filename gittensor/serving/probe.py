# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared probing helpers for talking to a serving runtime over HTTP.

Used by ``scripts/build_serving_audit_bank.py`` (build the validator's audit bank) and
``scripts/check_serving_runtime.py`` (contract conformance). Kept out of the validator path: the
validator never calls a runtime directly, it only verifies miner responses against the bank.
"""

import random
from typing import Dict, List, Optional

import requests

SUBJECTS = [
    'a Rust borrow checker error',
    'photosynthesis',
    'the French Revolution',
    'a sourdough starter',
    'TCP congestion control',
    'the offside rule',
    'a binary search tree',
    'monsoon seasons',
    'a 401(k)',
    'the Pythagorean theorem',
    'a CUDA kernel',
    'Git rebase',
    'the water cycle',
    'a haiku',
    'the Krebs cycle',
    'a sonnet',
    'a mortgage amortization table',
    'Kubernetes pods',
    'the Great Barrier Reef',
    'a TCP handshake',
    'an LRU cache',
    'the electoral college',
    'a Bittensor subnet',
    'the Doppler effect',
    'a chess opening',
    'regular expressions',
]
TEMPLATES = [
    'Explain {s} in two sentences.',
    'Give three bullet points about {s}.',
    'Write a one-paragraph summary of {s} for a ten-year-old.',
    'What is a common misconception about {s}?',
    'List two pros and two cons of {s}.',
    'Describe {s} using an analogy.',
    'Write a short Python function related to {s}.',
    'Compose a limerick about {s}.',
]
SYSTEMS = [None, 'You are a concise assistant.', 'Answer in plain English.', 'Be precise and brief.']


def make_prompts(count: int, seed: int) -> List[List[Dict[str, str]]]:
    rng = random.Random(seed)
    prompts = []
    for _ in range(count):
        content = rng.choice(TEMPLATES).format(s=rng.choice(SUBJECTS))
        system = rng.choice(SYSTEMS)
        msgs = [{'role': 'system', 'content': system}] if system else []
        msgs.append({'role': 'user', 'content': content})
        prompts.append(msgs)
    return prompts


def auth_headers(api_key: Optional[str]) -> Dict[str, str]:
    return {'Authorization': f'Bearer {api_key}'} if api_key else {}


def greedy(
    base_url: str,
    model_id: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    timeout: float,
    api_key: Optional[str] = None,
) -> dict:
    r = requests.post(
        f'{base_url.rstrip("/")}/v1/chat/completions',
        headers=auth_headers(api_key),
        json={
            'model': model_id,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': 0,
            'stream': False,
            'logprobs': True,
            'top_logprobs': 1,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    choice = payload['choices'][0]
    content = (choice.get('logprobs') or {}).get('content') or []
    if not content:
        raise RuntimeError('server returned no logprobs; sparkinfer_server must be built with logprobs support')
    return {
        'messages': messages,
        'max_tokens': max_tokens,
        'reference_tokens': [e['token'] for e in content],
        'reference_logprobs': [float(e['logprob']) for e in content],
        'reference_completion': choice['message'].get('content') or '',
        'served_model': payload.get('model'),
        'generation_ms': payload.get('generation_ms'),
    }


def score(
    base_url: str,
    model_id: str,
    messages: List[Dict[str, str]],
    completion: str,
    timeout: float,
    api_key: Optional[str] = None,
    top_logprobs: int = 1,
) -> dict:
    """Teacher-forced scoring (contract R8): per-token logprobs of ``completion`` under the model.

    Returns ``tokens``, ``logprobs`` and, when ``top_logprobs`` > 0, ``argmax`` (the model's own
    top token at each position) so a verifier can compare any miner output position by position.
    """
    r = requests.post(
        f'{base_url.rstrip("/")}/v1/score',
        headers=auth_headers(api_key),
        json={'model': model_id, 'messages': messages, 'completion': completion, 'top_logprobs': top_logprobs},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    out = {
        'tokens': list(payload['tokens']),
        'logprobs': [float(x) for x in payload['logprobs']],
        'usage': payload.get('usage') or {},
    }
    if top_logprobs > 0 and payload.get('top_logprobs'):
        out['argmax'] = [(alts[0]['token'] if alts else None) for alts in payload['top_logprobs']]
    return out


def stability(a: dict, b: dict) -> tuple[float, float]:
    prefix = 0
    for x, y in zip(a['reference_tokens'], b['reference_tokens']):
        if x != y:
            break
        prefix += 1
    agreement = prefix / max(1, len(a['reference_tokens']))
    diffs = [abs(x - y) for x, y in zip(a['reference_logprobs'][:prefix], b['reference_logprobs'][:prefix])]
    return agreement, (sum(diffs) / len(diffs) if diffs else 0.0)


def percentile(xs: List[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round((p / 100.0) * (len(s) - 1))))
    return s[k]
