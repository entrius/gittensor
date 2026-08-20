#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Build the serving audit bank from a trusted sparkinfer_server.

Run on a team 5090 with the pinned runtime + model:

    ./server/run.sh --download --port 8080            # in the sparkinfer checkout
    uv run python scripts/build_serving_audit_bank.py \\
        --base-url http://127.0.0.1:8080 --model-id qwen3.6-35b-a3b \\
        --runtime-pin gittensor-ai-lab/sparkinfer@<commit> --count 500 --max-tokens 64 \\
        --out gittensor/validator/weights/serving_audit_bank.json

Each case is {messages, max_tokens, reference_tokens, reference_logprobs,
reference_completion} from a greedy decode. Prompts are drawn from a seeded
template pool so the bank is reproducible; bump --seed to rotate it. Re-run
whenever the runtime pin or model changes — the validator refuses a bank whose
model_id does not match the loadout.

Pass --repeat N to measure the runtime's own greedy stability (same prompt N
times): the printed prefix-agreement / logprob-drift distribution is what the
SERVING_AUDIT_* tolerance constants must be calibrated against.
"""

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List

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


def greedy(base_url: str, model_id: str, messages: List[Dict[str, str]], max_tokens: int, timeout: float) -> dict:
    r = requests.post(
        f'{base_url.rstrip("/")}/v1/chat/completions',
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


def stability(a: dict, b: dict) -> tuple[float, float]:
    prefix = 0
    for x, y in zip(a['reference_tokens'], b['reference_tokens']):
        if x != y:
            break
        prefix += 1
    agreement = prefix / max(1, len(a['reference_tokens']))
    diffs = [abs(x - y) for x, y in zip(a['reference_logprobs'][:prefix], b['reference_logprobs'][:prefix])]
    return agreement, (sum(diffs) / len(diffs) if diffs else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base-url', default='http://127.0.0.1:8080')
    ap.add_argument('--model-id', required=True)
    ap.add_argument('--runtime-pin', required=True)
    ap.add_argument('--count', type=int, default=500)
    ap.add_argument('--max-tokens', type=int, default=64)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument(
        '--repeat', type=int, default=0, help='re-run each prompt N extra times and report greedy stability'
    )
    ap.add_argument('--out', type=Path, default=Path('gittensor/validator/weights/serving_audit_bank.json'))
    args = ap.parse_args()

    prompts = make_prompts(args.count, args.seed)
    cases = []
    agreements: List[float] = []
    drifts: List[float] = []
    t0 = time.time()
    for i, messages in enumerate(prompts, 1):
        case = greedy(args.base_url, args.model_id, messages, args.max_tokens, args.timeout)
        for _ in range(args.repeat):
            again = greedy(args.base_url, args.model_id, messages, args.max_tokens, args.timeout)
            agreement, drift = stability(case, again)
            agreements.append(agreement)
            drifts.append(drift)
        cases.append(
            {
                k: case[k]
                for k in ('messages', 'max_tokens', 'reference_tokens', 'reference_logprobs', 'reference_completion')
            }
        )
        if i % 25 == 0 or i == len(prompts):
            print(f'  {i}/{len(prompts)} cases ({time.time() - t0:.0f}s)', file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                'model_id': args.model_id,
                'runtime_pin': args.runtime_pin,
                'base_url': args.base_url,
                'seed': args.seed,
                'max_tokens': args.max_tokens,
                'built_at': int(time.time()),
                'cases': cases,
            },
            indent=1,
        )
    )
    print(f'wrote {len(cases)} cases -> {args.out}')

    if agreements:
        print('\nGreedy stability across repeats (calibrate SERVING_AUDIT_* against this):')
        print(
            f'  prefix agreement: min {min(agreements):.3f}  p05 {_pct(agreements, 5):.3f}  median {statistics.median(agreements):.3f}'
        )
        print(
            f'  logprob drift   : max {max(drifts):.4f}  p95 {_pct(drifts, 95):.4f}  median {statistics.median(drifts):.4f}'
        )
    return 0


def _pct(xs: List[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round((p / 100.0) * (len(s) - 1))))
    return s[k]


if __name__ == '__main__':
    sys.exit(main())
