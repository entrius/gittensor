"""Planted-cheater experiment for the serving verifier.

Measures how well the audit metrics separate an honest miner (same release as the reference) from
planted cheaters (cheaper quant, different runtime). Run `record` against the reference server, then
`score` against each candidate. Results from 2026-08-22 are in docs/serving-runtime-contract.md §4.2.

record: run N audit prompts against an (honest) server, save reference tokens+logprobs.
score : run the same prompts against a candidate server, report the metrics verify_response uses
        (prefix agreement, mean |dlogprob| over agreed prefix) plus positional token overlap.
"""

import argparse
import json
import statistics
import sys
import time

from gittensor.serving.probe import greedy, make_prompts, percentile


def metrics(ref, cand):
    rt, rl = ref['reference_tokens'], ref['reference_logprobs']
    ct, cl = cand['reference_tokens'], cand['reference_logprobs']
    prefix = 0
    for a, b in zip(ct, rt):
        if a != b:
            break
        prefix += 1
    agreement = prefix / max(1, len(rt))
    diffs = [abs(a - b) for a, b in zip(cl[:prefix], rl[:prefix])]
    mean_diff = sum(diffs) / len(diffs) if diffs else float('inf')
    overlap = sum(1 for a, b in zip(ct, rt) if a == b) / max(1, len(rt))
    return {'prefix_agreement': agreement, 'mean_abs_logprob_diff': mean_diff, 'positional_overlap': overlap}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['record', 'score'])
    ap.add_argument('--base-url', default='http://127.0.0.1:8080')
    ap.add_argument('--model-id', required=True)
    ap.add_argument('--count', type=int, default=40)
    ap.add_argument('--seed', type=int, default=4242)
    ap.add_argument('--max-tokens', type=int, default=64)
    ap.add_argument('--ref', default='ref.json')
    ap.add_argument('--out', required=True)
    ap.add_argument('--label', default='')
    ap.add_argument(
        '--drop-first',
        action='store_true',
        help='candidate runtime emits the first-token logprob; sparkinfer reference does not',
    )
    a = ap.parse_args()

    if a.mode == 'record':
        prompts = make_prompts(a.count, seed=a.seed)
        cases = []
        for i, msgs in enumerate(prompts):
            cases.append(greedy(a.base_url, a.model_id, msgs, a.max_tokens, 120))
            print(f'record {i + 1}/{len(prompts)} tokens={len(cases[-1]["reference_tokens"])}', file=sys.stderr)
        json.dump({'seed': a.seed, 'max_tokens': a.max_tokens, 'cases': cases}, open(a.out, 'w'))
        return

    ref = json.load(open(a.ref))
    rows = []
    for i, case in enumerate(ref['cases']):
        t0 = time.time()
        cand = greedy(a.base_url, a.model_id, case['messages'], ref['max_tokens'] + (1 if a.drop_first else 0), 120)
        if a.drop_first:
            cand['reference_tokens'] = cand['reference_tokens'][1:]
            cand['reference_logprobs'] = cand['reference_logprobs'][1:]
        m = metrics(case, cand)
        m['ms'] = round((time.time() - t0) * 1000)
        rows.append(m)
        print(
            f'score {i + 1}/{len(ref["cases"])} agree={m["prefix_agreement"]:.2f} drift={m["mean_abs_logprob_diff"]:.3f} overlap={m["positional_overlap"]:.2f}',
            file=sys.stderr,
        )

    def summ(k):
        xs = [r[k] for r in rows if r[k] != float('inf')]
        return {
            'min': min(xs),
            'p05': percentile(xs, 5),
            'median': statistics.median(xs),
            'p95': percentile(xs, 95),
            'max': max(xs),
            'n_inf': len(rows) - len(xs),
        }

    summary = {k: summ(k) for k in ('prefix_agreement', 'mean_abs_logprob_diff', 'positional_overlap')}
    # pass rate under current constants
    summary['pass_rate_0.80_0.50'] = sum(
        1 for r in rows if r['prefix_agreement'] >= 0.80 and r['mean_abs_logprob_diff'] <= 0.50
    ) / len(rows)
    summary['diverged_first_token'] = sum(1 for r in rows if r['prefix_agreement'] == 0) / len(rows)
    json.dump({'label': a.label, 'rows': rows, 'summary': summary}, open(a.out, 'w'), indent=1)
    print(json.dumps({'label': a.label, 'summary': summary}, indent=1))


if __name__ == '__main__':
    main()
