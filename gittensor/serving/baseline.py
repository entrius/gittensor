# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Baseline prompts: what the validator sends serving miners when real traffic does not cover them.

Served traffic is the only audit, so every miner needs a few verified requests per round whether or not a user
showed up. These prompts only have to satisfy one thing: the reference can answer them (any text can be greedy
decoded). They are template-shaped and a miner that wants to can recognise them; what stops it profiting from that
is that a refusal or a wrong answer on any *other* request is judged the same way (a budget refusal is neutral only
by the validator's own ledger; see ``forward.py``), so answering these and nothing else earns nothing extra. Mixing
real user prompts into the baseline is not built.
"""

import random
from typing import Dict, List

Message = Dict[str, str]

TOPICS = [
    'a rate limiter for an HTTP API',
    'retry with exponential backoff',
    'an LRU cache',
    'parsing a CSV with quotes',
    'binary search over a sorted list',
    'merging two sorted iterators',
    'a token bucket',
    'validating an email',
    'a background job queue',
    'reading a large file in chunks',
    'a simple state machine',
    'a debounce helper',
    'a connection pool',
    'a trie for prefix search',
    'topological sort of a dependency graph',
    'a bloom filter',
    'a priority queue with decrease-key',
    'a sliding-window rate counter',
    'a cron expression parser',
    'a JSON pointer resolver',
    'a diff of two dictionaries',
    'a circuit breaker',
    'a retry-safe idempotency key',
    'a fixed-point number type',
    'a pagination cursor',
    'a semantic version comparator',
    'a URL router',
    'a streaming SSE parser',
    'an interval tree',
    'a consistent-hash ring',
]
LANGS = ['Python', 'TypeScript', 'Go', 'Rust', 'Java', 'C#', 'Kotlin', 'Ruby']
SNIPPETS = [
    'def handle(req):\n    if not req.ok:\n        return None\n    items = [x for x in req.items if x.active]\n'
    '    return sorted(items, key=lambda x: x.ts)',
    'export async function load(id: string) {\n  const r = await fetch(`/api/${id}`);\n  if (!r.ok) throw new Error(r.statusText);\n'
    '  return r.json();\n}',
    'func Sum(xs []int) int {\n\ts := 0\n\tfor _, x := range xs {\n\t\ts += x\n\t}\n\treturn s\n}',
    "fn parse(line: &str) -> Option<(u32, &str)> {\n    let (a, b) = line.split_once(',')?;\n    Some((a.parse().ok()?, b))\n}",
    'class Cache:\n    def __init__(self, n):\n        self.n = n\n        self.d = {}\n    def get(self, k):\n'
    '        return self.d.get(k)',
    'SELECT u.id, count(o.id) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.id HAVING count(o.id) > 3;',
    'for f in *.log; do grep -c ERROR "$f" | xargs -I{} echo "$f {}"; done',
]
PROSE = [
    'Summarize the trade-offs between optimistic and pessimistic locking for a booking system.',
    'Explain, for a new team member, why we pin dependency versions in CI but not in the library itself.',
    'Draft a short incident note: the queue backed up for 40 minutes because a consumer deployed with the wrong env.',
    'List the questions you would ask before migrating a service from REST to gRPC.',
    'Write release notes for a change that makes retries idempotent and adds a dead-letter queue.',
    'Compare two ways of storing feature flags and recommend one for a 5-person team.',
]


NAMES = ['atlas', 'beacon', 'cobalt', 'delta', 'ember', 'fjord', 'granite', 'harbor', 'iris', 'juniper', 'kestrel']


def make_baseline_prompt(rng: random.Random) -> List[Message]:
    kind = rng.random()
    topic, lang = rng.choice(TOPICS), rng.choice(LANGS)
    project = f'{rng.choice(NAMES)}-{rng.randint(2, 99)}'
    if kind < 0.3:
        content = (
            f'In the {project} service, write {topic} in {lang}. Include a short docstring and one example call'
            + rng.choice(
                [
                    '.',
                    f'; the caller passes at most {rng.randint(3, 500)} items.',
                    f' (target version {rng.randint(1, 4)}.{rng.randint(0, 20)}).',
                ]
            )
        )
    elif kind < 0.65:
        lines = '\n'.join(f'{i + 1}: {rng.choice(SNIPPETS)}' for i in range(rng.randint(4, 40)))
        ask = rng.choice(
            [
                'Explain what this does and list two bugs.',
                'Which of these would you refactor first, and why?',
                'Add error handling to the weakest function.',
                'Write unit tests for the second block.',
            ]
        )
        content = f'Here is a file:\n{lines}\n\n{ask}'
    elif kind < 0.85:
        content = (
            f'You are reviewing a pull request that adds {topic} in {lang}. The diff touches '
            f'{rng.randint(2, 12)} files and {rng.randint(20, 900)} lines. Summarize the risks in three bullet points.'
        )
    else:
        content = (
            f'Context: the {project} service, {rng.randint(2, 40)} engineers. '
            + rng.choice(PROSE)
            + (f' Keep it under {rng.choice([80, 150, 300])} words.' if rng.random() < 0.5 else '')
        )
    return [{'role': 'user', 'content': content}]


def baseline_max_tokens(rng: random.Random, cap: int) -> int:
    """Mostly short-to-medium answers, occasionally long — the shape of agent traffic."""
    return min(cap, rng.choice([64, 96, 128, 128, 192, 256, 256, 384, 512]))
