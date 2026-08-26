#!/usr/bin/env python3
"""Baseline traffic for the serving gateway: keeps every miner audited when real traffic is quiet.

Served traffic is the only audit the validator runs, so with no users a miner would coast on a stale window and a
new miner could never earn one. This client sends realistic, varied prompts through the gateway at a steady rate
using a key from SERVING_BASELINE_API_KEYS, which lets the gateway route to probation (not yet READY) miners too.

    export KEY=<baseline key>
    python3 scripts/serving_baseline_traffic.py --base-url http://127.0.0.1:8790 --rate 12 --duration 300

Cron it on the validator host (e.g. every 5 minutes with --duration 290). Stdlib only.
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

TOPICS = [
    'a rate limiter for an HTTP API',
    'retry with exponential backoff',
    'a LRU cache',
    'parsing a CSV with quotes',
    'a binary search over a sorted list',
    'merging two sorted iterators',
    'a token bucket',
    'validating an email',
    'a background job queue',
    'reading a large file in chunks',
    'a simple state machine',
    'a debounce helper',
]
LANGS = ['Python', 'TypeScript', 'Go', 'Rust']
SNIPPET = (
    'def handle(req):\n    if not req.ok:\n        return None\n    items = [x for x in req.items if x.active]\n'
    '    return sorted(items, key=lambda x: x.ts)\n'
)


def make_prompt(rng: random.Random) -> list:
    kind = rng.random()
    topic, lang = rng.choice(TOPICS), rng.choice(LANGS)
    if kind < 0.35:
        content = f'Write {topic} in {lang}. Include a short docstring and one example call.'
    elif kind < 0.7:
        filler = ' '.join(f'line {i}: {SNIPPET.strip()}' for i in range(rng.randint(5, 60)))
        content = f'Here is a file:\n{filler}\n\nExplain what handle() does and list two bugs.'
    else:
        content = (
            f'You are reviewing a pull request that adds {topic} in {lang}. The diff touches '
            f'{rng.randint(2, 9)} files. Summarize the risks in three bullet points.'
        )
    return [{'role': 'user', 'content': content}]


def one(base: str, key: str, rng: random.Random, timeout: float) -> dict:
    body = json.dumps(
        {
            'messages': make_prompt(rng),
            'max_tokens': rng.choice([64, 128, 256, 512]),
            'stream': True,
        }
    ).encode()
    req = urllib.request.Request(
        base.rstrip('/') + '/v1/chat/completions',
        body,
        {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            served = None
            for raw in r:
                if raw.startswith(b'data:') and b'served_uid' in raw:
                    served = json.loads(raw[5:]).get('gittensor', {}).get('served_uid')
            return {'status': r.status, 's': round(time.time() - t0, 2), 'uid': served}
    except urllib.error.HTTPError as e:
        return {'status': e.code, 's': round(time.time() - t0, 2)}
    except Exception as e:
        return {'status': type(e).__name__, 's': round(time.time() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-url', default='http://127.0.0.1:8790')
    ap.add_argument('--rate', type=float, default=12.0, help='requests per minute')
    ap.add_argument('--duration', type=float, default=290.0, help='seconds to run; 0 = one request')
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--seed', type=int, default=None)
    args = ap.parse_args()
    key = os.environ.get('KEY')
    if not key:
        print('set KEY to a SERVING_BASELINE_API_KEYS key', file=sys.stderr)
        return 2
    rng = random.Random(args.seed)
    started = time.time()
    interval = 60.0 / max(args.rate, 1e-9)
    while True:
        res = one(args.base_url, key, rng, args.timeout)
        print(json.dumps({'ts': round(time.time(), 1), **res}), flush=True)
        if args.duration <= 0 or time.time() - started + interval > args.duration:
            return 0
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
