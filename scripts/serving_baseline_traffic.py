#!/usr/bin/env python3
"""External baseline traffic for the serving gateway (optional; the validator sends its own baseline in-process).

Useful for load tests and for exercising the gateway path end to end from another host. Uses the same prompt corpus
as the validator (gittensor/serving/baseline.py); a key from SERVING_BASELINE_API_KEYS lets the gateway route to
probation (not yet READY) miners too.

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from gittensor.serving.baseline import baseline_max_tokens, make_baseline_prompt  # noqa: E402


def one(base: str, key: str, rng: random.Random, timeout: float) -> dict:
    body = json.dumps(
        {'messages': make_baseline_prompt(rng), 'max_tokens': baseline_max_tokens(rng, 1024), 'stream': True}
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
