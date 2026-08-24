#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Conformance checker for the Gittensor Serving Runtime Contract (docs/serving-runtime-contract.md).

Point it at a running runtime and it exercises every MUST/SHOULD in the contract:

    uv run python scripts/check_serving_runtime.py --base-url http://127.0.0.1:8080 --model-id qwen3.6-35b-a3b

Prints PASS/FAIL/WARN per check, the measured greedy-stability distribution (D1) and whether the
server 429s under overload instead of queueing (R6). Exit code 1 if any MUST fails. Runtime
maintainers run this before claiming conformance; miners run it before registering.
"""

import argparse
import concurrent.futures
import statistics
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

from gittensor.serving.probe import auth_headers, greedy, make_prompts, percentile, score, stability

MUST, SHOULD = 'MUST', 'SHOULD'


class Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, bool, str]] = []

    def add(self, check: str, level: str, ok: bool, detail: str = '') -> None:
        self.rows.append((check, level, ok, detail))
        tag = 'PASS' if ok else ('FAIL' if level == MUST else 'WARN')
        print(f'[{tag}] {check:<34} {detail}')

    @property
    def must_failures(self) -> int:
        return sum(1 for _, level, ok, _ in self.rows if level == MUST and not ok)


def get_json(url: str, timeout: float, api_key: Optional[str] = None) -> Tuple[Optional[int], Optional[dict]]:
    try:
        r = requests.get(url, timeout=timeout, headers=auth_headers(api_key))
        return r.status_code, (r.json() if r.content else {})
    except requests.RequestException:
        return None, None
    except ValueError:
        return r.status_code, None


API_KEY: Optional[str] = None


def chat(base_url: str, body: Dict, timeout: float) -> requests.Response:
    return requests.post(
        f'{base_url.rstrip("/")}/v1/chat/completions', json=body, timeout=timeout, headers=auth_headers(API_KEY)
    )


def check_models(rep: Report, base_url: str, model_id: str, timeout: float) -> None:
    status, payload = get_json(f'{base_url}/v1/models', timeout, API_KEY)
    ids = [m.get('id') for m in (payload or {}).get('data', [])] if payload else []
    rep.add('3.2 GET /v1/models', MUST, status == 200 and bool(ids), f'status={status} ids={ids}')
    rep.add('3.2 model_id advertised', MUST, model_id in ids, f'want {model_id!r}')
    rep.add('3.2 exactly one model', SHOULD, len(ids) == 1, f'{len(ids)} models')


def check_optional_endpoints(rep: Report, base_url: str, timeout: float) -> None:
    status, payload = get_json(f'{base_url}/v1/capacity', timeout, API_KEY)
    rep.add('3.3 GET /v1/capacity', SHOULD, status == 200 and isinstance(payload, dict), f'status={status} {payload}')
    status, _ = get_json(f'{base_url}/health', timeout, API_KEY)
    rep.add('3 GET /health', SHOULD, status == 200, f'status={status}')
    try:
        status = requests.get(f'{base_url}/metrics', timeout=timeout, headers=auth_headers(API_KEY)).status_code
    except requests.RequestException:
        status = None
    rep.add('3 GET /metrics', SHOULD, status == 200, f'status={status}')


def check_completion_shape(rep: Report, base_url: str, model_id: str, max_tokens: int, timeout: float) -> None:
    body = {
        'model': model_id,
        'messages': [{'role': 'user', 'content': 'Explain TCP congestion control in two sentences.'}],
        'max_tokens': max_tokens,
        'temperature': 0,
        'stream': False,
        'logprobs': True,
        'top_logprobs': 1,
    }
    try:
        r = chat(base_url, body, timeout)
    except requests.RequestException as exc:
        rep.add('3.1 POST /v1/chat/completions', MUST, False, str(exc))
        return
    rep.add('3.1 POST /v1/chat/completions', MUST, r.status_code == 200, f'status={r.status_code}')
    if r.status_code != 200:
        return
    p = r.json()
    choice = (p.get('choices') or [{}])[0]
    content = choice.get('message', {}).get('content')
    usage = p.get('usage') or {}
    rep.add('R3 choices[0].message.content', MUST, isinstance(content, str) and bool(content))
    rep.add(
        'R3 finish_reason', MUST, choice.get('finish_reason') in ('stop', 'length'), repr(choice.get('finish_reason'))
    )
    rep.add(
        'R3 usage block',
        MUST,
        all(k in usage for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')),
        str(usage),
    )
    rep.add('R3 top-level model', MUST, p.get('model') == model_id, repr(p.get('model')))

    lp = (choice.get('logprobs') or {}).get('content')
    ok_lp = isinstance(lp, list) and bool(lp) and all('token' in e and 'logprob' in e for e in lp)
    rep.add('R2 logprobs.content present', MUST, ok_lp, f'{len(lp) if isinstance(lp, list) else 0} entries')
    if ok_lp:
        assert lp is not None
        rep.add('R2 logprob values <= 0', MUST, all(float(e['logprob']) <= 1e-6 for e in lp))
        n_ct = usage.get('completion_tokens') or 0
        rep.add(
            'R2 entries == completion_tokens',
            MUST,
            len(lp) == n_ct,
            f'{len(lp)} vs {n_ct}'
            + (' (first-token logprob missing — fixed upstream in 9e43bfa)' if len(lp) == n_ct - 1 else ''),
        )
    rep.add(
        'R4 max_tokens honoured',
        MUST,
        usage.get('completion_tokens', 0) <= max_tokens
        and (choice.get('finish_reason') == 'length' or usage.get('completion_tokens', 0) < max_tokens),
        f'completion_tokens={usage.get("completion_tokens")} max={max_tokens}',
    )
    for field in ('ttft_ms', 'generation_ms', 'decode_tps'):
        val = p.get(field, usage.get(field))  # sparkinfer nests them under usage
        rep.add(f'R5 {field}', SHOULD, isinstance(val, (int, float)), repr(val))


def check_determinism(
    rep: Report, base_url: str, model_id: str, count: int, repeat: int, max_tokens: int, timeout: float
) -> None:
    agreements: List[float] = []
    drifts: List[float] = []
    t0 = time.time()
    for i, messages in enumerate(make_prompts(count, seed=7), 1):
        first = greedy(base_url, model_id, messages, max_tokens, timeout, API_KEY)
        for _ in range(repeat):
            agreement, drift = stability(first, greedy(base_url, model_id, messages, max_tokens, timeout, API_KEY))
            agreements.append(agreement)
            drifts.append(drift)
        if i % 10 == 0 or i == count:
            print(f'      determinism {i}/{count} ({time.time() - t0:.0f}s)', file=sys.stderr)
    p05 = percentile(agreements, 5)
    p95 = percentile(drifts, 95)
    rep.add(
        'D1 prefix agreement p05 >= 0.99',
        MUST,
        p05 >= 0.99,
        f'min {min(agreements):.3f} p05 {p05:.3f} median {statistics.median(agreements):.3f}',
    )
    rep.add(
        'D1 logprob drift p95 <= 0.01',
        MUST,
        p95 <= 0.01,
        f'max {max(drifts):.4f} p95 {p95:.4f} median {statistics.median(drifts):.4f}',
    )
    print('      -> a deterministic runtime reports p05 1.000 / p95 0.0000 (sparkinfer SPARKINFER_DETERMINISTIC=1).')


def check_score(rep: Report, base_url: str, model_id: str, max_tokens: int, timeout: float) -> None:
    """R8: /v1/score must exist and reproduce, for the model's own greedy output, the logprobs generation reported."""
    messages = make_prompts(1, seed=11)[0]
    gen = greedy(base_url, model_id, messages, max_tokens, timeout, API_KEY)
    try:
        sc = score(base_url, model_id, messages, gen['reference_completion'], timeout, API_KEY)
    except requests.HTTPError as e:
        rep.add('R8 POST /v1/score', MUST, False, f'HTTP {e.response.status_code if e.response else "?"}')
        return
    rep.add('R8 POST /v1/score', MUST, True, f'{len(sc["tokens"])} tokens')
    same_tok = sc['tokens'] == gen['reference_tokens']
    rep.add(
        'R8 scored tokens == generated tokens', MUST, same_tok, f'{len(sc["tokens"])} vs {len(gen["reference_tokens"])}'
    )
    if same_tok:
        diff = max(abs(a - b) for a, b in zip(sc['logprobs'], gen['reference_logprobs']))
        rep.add('R8 scored logprobs == generated logprobs', MUST, diff <= 1e-4, f'max |delta| {diff:.6f}')
    if sc.get('argmax'):
        agree = sum(1 for a, b in zip(sc['argmax'], gen['reference_tokens']) if a == b) / len(gen['reference_tokens'])
        rep.add('R8 top_logprobs argmax == greedy token', SHOULD, agree >= 0.99, f'{agree:.3f}')


def check_overload(rep: Report, base_url: str, model_id: str, parallel: int, max_tokens: int, timeout: float) -> None:
    """Fire `parallel` long requests at once; a conformant runtime 429s the excess instead of queueing."""
    body = {
        'model': model_id,
        'messages': [{'role': 'user', 'content': 'Write a long essay about the history of computing.'}],
        'max_tokens': max_tokens,
        'temperature': 0,
        'stream': False,
    }

    def one() -> Tuple[Optional[int], float]:
        t = time.monotonic()
        try:
            return chat(base_url, body, timeout).status_code, time.monotonic() - t
        except requests.RequestException:
            return None, time.monotonic() - t

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        results = list(pool.map(lambda _: one(), range(parallel)))
    codes = [c for c, _ in results]
    n429 = codes.count(429)
    n200 = codes.count(200)
    ok_latencies = sorted(t for c, t in results if c == 200)
    # Heuristic for queueing: if everything returned 200 and the slowest successful request took
    # more than ~2x the fastest, the server serialised requests behind each other.
    queued = n429 == 0 and len(ok_latencies) > 1 and ok_latencies[-1] > 2.0 * ok_latencies[0]
    rep.add(
        'R6 429 under overload (no queue)',
        MUST,
        n429 > 0 or not queued,
        f'{parallel} parallel: {n200}x200 {n429}x429 other={[c for c in codes if c not in (200, 429)]}'
        + (f' fastest {ok_latencies[0]:.1f}s slowest {ok_latencies[-1]:.1f}s' if ok_latencies else ''),
    )
    if n429 == 0:
        print(
            f"      no 429 seen at {parallel} parallel requests — raise --parallel above the runtime's slot count "
            'to confirm it rejects rather than queues.'
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base-url', default='http://127.0.0.1:8080')
    ap.add_argument('--model-id', required=True)
    ap.add_argument('--max-tokens', type=int, default=64)
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--determinism-count', type=int, default=30, help='prompts for the D1 stability probe (0 to skip)')
    ap.add_argument('--repeat', type=int, default=3, help='extra runs per prompt for D1')
    ap.add_argument(
        '--parallel', type=int, default=16, help='concurrent requests for the R6 overload probe (0 to skip)'
    )
    ap.add_argument('--overload-max-tokens', type=int, default=512)
    ap.add_argument('--api-key', default=None, help='bearer for a remote runtime (sparkinfer --api-key)')
    args = ap.parse_args()
    global API_KEY
    API_KEY = args.api_key
    base_url = args.base_url.rstrip('/')

    rep = Report()
    print(f'Serving runtime contract v0 — checking {base_url} for model {args.model_id!r}\n')
    check_models(rep, base_url, args.model_id, args.timeout)
    check_optional_endpoints(rep, base_url, args.timeout)
    check_completion_shape(rep, base_url, args.model_id, args.max_tokens, args.timeout)
    check_score(rep, base_url, args.model_id, args.max_tokens, args.timeout)
    if args.determinism_count > 0:
        check_determinism(
            rep, base_url, args.model_id, args.determinism_count, args.repeat, args.max_tokens, args.timeout
        )
    if args.parallel > 0:
        check_overload(rep, base_url, args.model_id, args.parallel, args.overload_max_tokens, args.timeout)

    failures = rep.must_failures
    print(
        f'\n{"CONFORMANT" if failures == 0 else "NOT CONFORMANT"}: {failures} MUST failure(s), '
        f'{sum(1 for _, lvl, ok, _ in rep.rows if lvl == SHOULD and not ok)} SHOULD warning(s)'
    )
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
