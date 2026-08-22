"""Planted-cheater experiment for the serving verifier.

Measures how well the audit metrics separate an honest miner (same release as the reference) from
planted cheaters (cheaper quant, different runtime). Run `record` against the reference server, then
`score` against each candidate. Results from 2026-08-22 are in docs/serving-runtime-contract.md §4.2.

record : run N audit prompts against an (honest) server, save reference tokens+logprobs.
score  : run the same prompts against a candidate server, report the metrics verify_response uses
         (prefix agreement, mean |dlogprob| over agreed prefix) plus positional token overlap.
analyze: from saved score files, print the comparison table, the honest-calibrated threshold per
         window size, and bootstrap detection power for each cheater.

Re-scoring a new candidate against the saved 2026-08-22 reference does not need a new reference:
    python scripts/serving_cheat_experiment.py score --ref docs/serving-experiments/2026-08-22-planted-cheater/reference.json ...
"""

import argparse
import json
import random
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


def analyze(honest_files, cheater_files, fp, windows, key='positional_overlap', draws=20000):
    """Threshold per window size at the given honest false-positive rate, and detection power per cheater."""
    rng = random.Random(1)

    def load(f):
        d = json.load(open(f))
        return d['label'], d['rows']

    honest = [load(f) for f in honest_files]
    cheaters = [load(f) for f in cheater_files]
    hrows = [r for _, rows in honest for r in rows]

    def mean_k(rows, k):
        return sum(rng.choice(rows)[key] for _ in range(k)) / k

    print(
        f'{"candidate":40s} {"prefix med":>10s} {"overlap med":>11s} {"drift med":>9s} {"drift max":>9s} {"ms med":>6s}'
    )
    for label, rows in honest + cheaters:
        fin = [r['mean_abs_logprob_diff'] for r in rows if r['mean_abs_logprob_diff'] != float('inf')]
        print(
            f'{label:40s} {statistics.median(r["prefix_agreement"] for r in rows):10.3f} '
            f'{statistics.median(r[key] for r in rows):11.3f} {statistics.median(fin):9.3f} {max(fin):9.3f} '
            f'{statistics.median(r["ms"] for r in rows):6.0f}'
        )
    print(f'\nmetric = mean {key} over the last k audits; threshold at {fp:.0%} honest false positives')
    print(f'{"k":>3s} {"threshold":>9s} ' + ' '.join(f'{label[:22]:>22s}' for label, _ in cheaters))
    for k in windows:
        hs = sorted(mean_k(hrows, k) for _ in range(draws))
        thr = hs[int(fp * draws)]
        power = [sum(mean_k(rows, k) < thr for _ in range(draws)) / draws for _, rows in cheaters]
        print(f'{k:3d} {thr:9.3f} ' + ' '.join(f'{x:22.3f}' for x in power))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['record', 'score', 'analyze'])
    ap.add_argument('--base-url', default='http://127.0.0.1:8080')
    ap.add_argument('--model-id')
    ap.add_argument('--count', type=int, default=40)
    ap.add_argument('--seed', type=int, default=4242)
    ap.add_argument('--max-tokens', type=int, default=64)
    ap.add_argument('--ref', default='ref.json')
    ap.add_argument('--out')
    ap.add_argument('--label', default='')
    ap.add_argument('--honest', nargs='*', default=[], help='analyze: score files from honest miners')
    ap.add_argument('--cheaters', nargs='*', default=[], help='analyze: score files from planted cheaters')
    ap.add_argument('--fp', type=float, default=0.01, help='analyze: honest false-positive rate to calibrate at')
    ap.add_argument('--windows', default='1,3,5,10,20,40', help='analyze: window sizes k to evaluate')
    ap.add_argument(
        '--drop-first',
        action='store_true',
        help='candidate runtime emits the first-token logprob; sparkinfer reference does not',
    )
    a = ap.parse_args()

    if a.mode == 'analyze':
        analyze(a.honest, a.cheaters, a.fp, [int(k) for k in a.windows.split(',')])
        return

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
