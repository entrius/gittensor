# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared probing helpers for talking to a serving runtime over HTTP.

Used by ``LiveReference`` (gittensor/serving/audit.py) to draw prompts and query the validator's
reference runtime, and by the scripts: ``build_serving_audit_bank.py`` (bank snapshot),
``check_serving_runtime.py`` (contract conformance) and ``serving_cheat_experiment.py``.
``compare`` is the one token/logprob comparison every verifier path shares.
"""

import random
from typing import Dict, List, Optional, Sequence, Tuple

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
    completion_token_ids: Optional[Sequence[int]] = None,
) -> dict:
    """Teacher-forced scoring (contract R8): per-token logprobs of ``completion`` under the model.

    With ``completion_token_ids`` the runtime forces exactly those tokens instead of re-tokenizing the text, so a
    miner's own greedy token sequence is scored position by position even where a fresh tokenization of the same
    text would pick different boundaries.

    Returns ``tokens``, ``logprobs`` and, when ``top_logprobs`` > 0, ``argmax`` (the model's own
    top token at each position) so a verifier can compare any miner output position by position.
    """
    r = requests.post(
        f'{base_url.rstrip("/")}/v1/score',
        headers=auth_headers(api_key),
        json={
            'model': model_id,
            'messages': messages,
            'top_logprobs': top_logprobs,
            **(
                {'completion_token_ids': list(completion_token_ids)}
                if completion_token_ids
                else {'completion': completion}
            ),
        },
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    out = {
        'tokens': list(payload['tokens']),
        'logprobs': [float(x) for x in payload['logprobs']],
        'usage': payload.get('usage') or {},
    }
    if payload.get('token_ids'):
        out['token_ids'] = [int(x) for x in payload['token_ids']]
    if payload.get('bytes'):
        out['bytes'] = [bytes(b) for b in payload['bytes']]
    if top_logprobs > 0 and payload.get('top_logprobs'):
        out['argmax'] = [(alts[0]['token'] if alts else None) for alts in payload['top_logprobs']]
    return out


def compare(
    tokens: Sequence[str],
    logprobs: Sequence[float],
    reference_tokens: Sequence[str],
    reference_logprobs: Sequence[float],
) -> Tuple[int, float, float, List[float]]:
    """Compare a candidate greedy output to a reference, position by position.

    Returns ``(prefix, agreement, overlap, diffs)``: tokens matched before the first divergence, that
    as a fraction of the reference length, the fraction of positions whose token matches ignoring
    divergence, and |logprob delta| per position over the agreed prefix.
    """
    prefix = 0
    for mine, ref in zip(tokens, reference_tokens):
        if mine != ref:
            break
        prefix += 1
    n_ref = max(1, len(reference_tokens))
    agreement = prefix / n_ref
    overlap = sum(1 for mine, ref in zip(tokens, reference_tokens) if mine == ref) / n_ref
    diffs = [abs(float(a) - float(b)) for a, b in zip(logprobs[:prefix], reference_logprobs[:prefix])]
    return prefix, agreement, overlap, diffs


def stability(a: dict, b: dict) -> Tuple[float, float]:
    """(prefix agreement, mean |logprob delta|) between two greedy runs of the same prompt."""
    _, agreement, _, diffs = compare(
        b['reference_tokens'], b['reference_logprobs'], a['reference_tokens'], a['reference_logprobs']
    )
    return agreement, (sum(diffs) / len(diffs) if diffs else 0.0)


def percentile(xs: List[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round((p / 100.0) * (len(s) - 1))))
    return s[k]
