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
import statistics
import sys
import time
from pathlib import Path
from typing import List

from gittensor.serving.probe import greedy, make_prompts, percentile, stability


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base-url', default='http://127.0.0.1:8080')
    ap.add_argument('--model-id', required=True)
    ap.add_argument('--runtime-pin', required=True)
    ap.add_argument('--count', type=int, default=500)
    ap.add_argument('--max-tokens', type=int, default=64)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--api-key', default=None, help='bearer for a remote runtime (sparkinfer --api-key)')
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
        case = greedy(args.base_url, args.model_id, messages, args.max_tokens, args.timeout, args.api_key)
        for _ in range(args.repeat):
            again = greedy(args.base_url, args.model_id, messages, args.max_tokens, args.timeout, args.api_key)
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
            f'  prefix agreement: min {min(agreements):.3f}  p05 {percentile(agreements, 5):.3f}  median {statistics.median(agreements):.3f}'
        )
        print(
            f'  logprob drift   : max {max(drifts):.4f}  p95 {percentile(drifts, 95):.4f}  median {statistics.median(drifts):.4f}'
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
