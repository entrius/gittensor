#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Conformance checker for the Gittensor Serving Runtime Contract (published with the miner docs).

Point it at a running runtime and it exercises every MUST/SHOULD in the contract:

    uv run python scripts/check_serving_runtime.py --base-url http://127.0.0.1:8080 --model-id qwen3.6-35b-a3b

Prints PASS/FAIL/WARN per check, the measured greedy-stability distribution (D1) and whether the
server 429s under overload instead of queueing (R6). Exit code 1 if any MUST fails. Runtime
maintainers run this before claiming conformance; miners run it before registering.
"""

import argparse
import concurrent.futures
import json
import random
import statistics
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

from gittensor.serving.audit import LiveReference, verify_served
from gittensor.serving.baseline import make_baseline_prompt
from gittensor.serving.loadout import ServingRelease
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
        # OpenAI counts the stop token in completion_tokens but omits it from logprobs.content (sparkinfer >= 12954e6).
        eos_omitted = choice.get('finish_reason') == 'stop' and len(lp) == n_ct - 1
        rep.add(
            'R2 entries == completion_tokens (or one fewer on stop: EOS omitted)',
            MUST,
            len(lp) == n_ct or eos_omitted,
            f'{len(lp)} vs {n_ct}' + (' (EOS omitted)' if eos_omitted else ''),
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
    argmax = sc.get('argmax') or []
    rep.add(
        'R8 /v1/score returns top_logprobs argmax', MUST, len(argmax) == len(sc['tokens']), f'{len(argmax)} entries'
    )
    if argmax:
        agree = sum(1 for a, b in zip(argmax, gen['reference_tokens']) if a == b) / len(gen['reference_tokens'])
        rep.add('R8 top_logprobs argmax == greedy token', MUST, agree >= 0.99, f'{agree:.3f}')
    # What the validator actually runs on served traffic: teacher-force the greedy output and demand a pass.
    release = ServingRelease(
        model_id=model_id,
        backend='openai-compat',
        max_tokens=max_tokens,
        reference_url=base_url,
        reference_api_key=API_KEY,
        request_timeout=timeout,
    )
    ref = LiveReference(release)
    verdict = verify_served(
        ref, messages, gen['reference_completion'], gen['reference_tokens'], gen['reference_logprobs']
    )
    rep.add("R8 verify_served passes the model's own greedy output", MUST, verdict.passed, verdict.reason)
    for i in range(3):  # and on longer, traffic-shaped prompts
        msgs = make_baseline_prompt(random.Random(100 + i))
        g = greedy(base_url, model_id, msgs, 256, timeout, API_KEY)
        v = verify_served(ref, msgs, g['reference_completion'], g['reference_tokens'], g['reference_logprobs'])
        rep.add(
            f'R8 verify_served on baseline prompt #{i + 1} ({len(g["reference_tokens"])} tok)', MUST, v.passed, v.reason
        )


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


def _stream_once(base_url: str, model_id: str, messages, max_tokens: int, timeout: float) -> Tuple[int, float, float]:
    """One streamed greedy request: (completion tokens, client-observed TTFT s, total wall s)."""
    from gittensor.serving.stream import SSEParser

    body = {
        'model': model_id,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': 0,
        'stream': True,
        'stream_options': {'include_usage': True},
    }
    parser = SSEParser()
    started = time.monotonic()
    ttft = None
    tokens = 0
    with requests.post(
        f'{base_url}/v1/chat/completions', headers=auth_headers(API_KEY), json=body, stream=True, timeout=timeout
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=None):
            if ttft is None:
                ttft = time.monotonic() - started
            for event in parser.feed(chunk):
                if event and isinstance(event.get('usage'), dict):
                    tokens = int(event['usage'].get('completion_tokens') or tokens)
    return tokens, ttft if ttft is not None else float('inf'), time.monotonic() - started


def speed_profile(base_url: str, model_id: str, max_tokens: int, timeout: float, burst: int, reps: int = 3) -> Dict:
    """Blessing-time speed facts for one honest card on this runtime, measured the way the validator measures.

    ``single_stream``: one request at a time. ``aggregate_decode_tps``: ``burst`` concurrent requests, aggregate
    verified tokens per second of decode time (batch wall-clock minus the first TTFT) — what one card serves flat
    out, so the per-token rate the validator pays at is SERVING_GPU_HOUR_USD over an hour of it. Client TTFT
    includes this client's RTT, so decode rates are RTT-free; the TTFT rows are informational unless the client is
    on-box.
    """
    prompts = make_prompts(burst * reps, seed=23)
    single = []
    for messages in prompts[:reps]:
        _stream_once(base_url, model_id, messages, 8, timeout)  # warm
        tokens, ttft, wall = _stream_once(base_url, model_id, messages, max_tokens, timeout)
        single.append((tokens / max(wall - ttft, 1e-3), ttft * 1000.0))
    aggregate = []
    per_request = []
    for i in range(reps):
        batch = prompts[i * burst : (i + 1) * burst]
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=burst) as pool:
            results = list(pool.map(lambda m: _stream_once(base_url, model_id, m, max_tokens, timeout), batch))
        wall = time.monotonic() - started
        tokens = sum(t for t, _, _ in results)
        first = min(ttft for _, ttft, _ in results)
        aggregate.append(tokens / max(wall - first, 1e-3))
        per_request.extend(t / max(w - tt, 1e-3) for t, tt, w in results)
    single_tps = statistics.median(t for t, _ in single)
    probe_tps = statistics.median(aggregate)
    curve = {1: round(single_tps, 1), burst: round(statistics.median(per_request), 1)}
    # Every step from 2 to 6 plus 8 and 12: the validator interpolates on aggregate rate between points, but the
    # 1 -> 2 knee (a batch forms and the aggregate drops before it flattens) is not interpolable from either side,
    # and 2-5 in flight is where every traffic burst spends its time (#1753).
    for k in (2, 3, 4, 5, 6, 8, 12):
        if k >= burst:
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=k) as pool:
            rk = list(pool.map(lambda m: _stream_once(base_url, model_id, m, max_tokens, timeout), prompts[:k]))
        curve[k] = round(statistics.median(t / max(w - tt, 1e-3) for t, tt, w in rk), 1)
    return {
        'single_stream_decode_tps': round(single_tps, 1),
        # what the release carries as speed.aggregate_decode_tps: one card's output tok/s under load
        'aggregate_decode_tps': round(probe_tps, 1),
        'burst_concurrency': burst,
        'client_ttft_ms_median': round(statistics.median(t for _, t in single), 1),
        'max_tokens': max_tokens,
        # concurrent requests -> per-request decode tok/s of one honest card: the validator prices a served request
        # against this curve at the load it had in flight to the miner
        'decode_per_request': {str(k): v for k, v in sorted(curve.items())},
    }


MODEL_RESIDENT_MIN_BYTES = 8_000_000_000


def check_attest(rep: Report, base_url: str, timeout: float, attest_url: Optional[str] = None) -> Dict:
    """A1: the attest container beside the runtime (entrius/gt-attest, :8081 on the runtime's host unless
    ``attest_url`` says otherwise) answers a seeded challenge with a deterministic digest inside 3 s idle — the
    admission check validators run; nothing counts cards."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(base_url)
    host = parts.hostname or ''
    attest_url = (attest_url or urlunsplit((parts.scheme or 'http', f'{host}:8081', '', '', ''))).rstrip('/')
    facts: Dict = {}
    try:
        single = requests.post(
            f'{attest_url}/v1/attest', json={'seed': 12345, 'iters': 3, 'fill': True}, timeout=timeout
        )
        single.raise_for_status()
        dev = (single.json().get('devices') or [single.json()])[0]
    except Exception as e:
        rep.add('A1 POST :8081/v1/attest', MUST, False, repr(e)[:120])
        return facts
    wall = float(dev.get('wall_ms') or 0.0)
    rep.add('A1 POST :8081/v1/attest', MUST, bool(dev.get('digest')), f'{wall:.0f} ms, uuid {dev.get("uuid")}')
    rep.add('A1 attest wall <= 3000 ms idle', MUST, 0 < wall <= 3000.0, f'{wall:.0f} ms')
    again = requests.post(f'{attest_url}/v1/attest', json={'seed': 12345, 'iters': 3, 'fill': True}, timeout=timeout)
    same = again.ok and (again.json().get('devices') or [again.json()])[0].get('digest') == dev.get('digest')
    rep.add('A1 digest deterministic for a seed', MUST, bool(same))
    facts.update(attest_ref_wall_ms=round(wall, 1), attest_iters=3)
    # What the model holds on a card is only measurable on a card that holds it. The conformance attest pod is a
    # bare gt-attest image, so its reservation is the attest server's own few hundred MB; writing that into the
    # release would make every honest miner "under-filled" (expected free ~= the whole card).
    reserved = int(dev.get('vram_total') or 0) - int(dev.get('vram_free_before') or 0)
    if reserved >= MODEL_RESIDENT_MIN_BYTES:
        facts['vram_model_reserved_bytes'] = reserved
    else:
        print(
            f'      attest card holds {reserved / 1e9:.1f} GB, no model resident: vram_model_reserved_bytes not measured'
        )
    return facts


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
    ap.add_argument('--attest-url', default=None, help='the attest container (default: the runtime host, port 8081)')
    ap.add_argument(
        '--speed-json',
        default=None,
        help='also measure the speed profile (single-stream and probe-shaped decode tok/s) and write it here',
    )
    ap.add_argument(
        '--speed-burst', type=int, default=16, help='concurrent requests in the saturation burst (blessed concurrency)'
    )
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

    attest_facts = check_attest(rep, base_url, args.timeout, args.attest_url)
    if args.speed_json:
        profile = speed_profile(base_url, args.model_id, args.max_tokens, args.timeout, args.speed_burst)
        profile['attest'] = attest_facts
        with open(args.speed_json, 'w') as f:
            json.dump(profile, f, indent=2)
        print(f'\nSpeed profile ({args.speed_json}): {profile}')

    failures = rep.must_failures
    print(
        f'\n{"CONFORMANT" if failures == 0 else "NOT CONFORMANT"}: {failures} MUST failure(s), '
        f'{sum(1 for _, lvl, ok, _ in rep.rows if lvl == SHOULD and not ok)} SHOULD warning(s)'
    )
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
