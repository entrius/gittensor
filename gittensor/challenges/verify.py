# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge verify``: the maintainer's verdict on a challenge PR. It never executes submitted code.

``decide`` is pure: it takes the facts (the PR, the repo's ``main``, the chain) and the repo's ``challenge.json`` and
runs the checks in order; the first failure decides. The fetchers gather those facts with ``gh api`` and a Subtensor;
``apply`` carries a verdict out with ``gh``, idempotently.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from functools import cache
from typing import Any

import click

from gittensor.challenges.attestation import DEV, Attestation, verify
from gittensor.challenges.head_to_head import Entry, verdict
from gittensor.challenges.runner import SeedResult

MAIN = 'main'
ATTESTATION = 'attestation.json'
NEEDS_REVIEW = 'needs-review'
WRITERS = ('admin', 'maintain', 'write')
BLOCK_SECONDS = 12
CLA_TEXT = 'I agree to the Contributor License Agreement in CLA.md and that this solver is licensed under LICENSING.md.'
CLA = re.compile(r'- \[[xX]\] ' + re.escape(CLA_TEXT))
NO_CLA = 'CLA not accepted: tick the agreement box and resubmit as a new PR'


@dataclass(frozen=True)
class Config:
    """``.gittensor/challenge.json`` on ``main``."""

    challenge_id: str
    module: str
    image: str | None
    tier: str
    seeds: int
    margin: float
    suspicious_gain: float
    freshness_blocks: int
    crown_label: str
    maintainers: list[str]
    dev_attestation_pubkey: str | None

    @classmethod
    def from_json(cls, text: str) -> Config:
        doc = json.loads(text)
        return cls(**{f.name: doc[f.name] for f in fields(cls)})


@dataclass(frozen=True)
class PullRequest:
    number: int
    author: str
    author_writes: bool
    actor: str  # who triggered the event
    actor_writes: bool
    state: str  # open, closed or merged
    ever_closed: bool
    commits: int
    files: list[str]
    created_at: datetime
    body: str = ''
    labels: list[str] = field(default_factory=list)
    attestation: str | None = None  # attestation.json at the head, as committed
    solver_sha: str | None = None  # the head's tree sha of the solver dir


@dataclass(frozen=True)
class Repo:
    """``main``: the crown and the queue."""

    king: str  # KING: the crowned solver's path
    king_sha: str
    leaderboard: str
    taken: list[str]  # the author's solver dirs already on main
    earlier_open: list[int]  # earlier PRs still in the queue


@dataclass(frozen=True)
class Chain:
    created_block: int  # the block the PR was opened in
    seed_block_hash: str | None  # the chain's hash at the attested seed block


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Verdict:
    pr: int
    decision: str  # crown, close, wait, needs_review or ignore
    reason: str
    checks: list[Check]
    round: int | None = None
    gain: float | None = None
    lower_99: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def decide(pr: PullRequest, repo: Repo, chain: Chain, config: Config) -> Verdict:
    """The G.3 checks in order: the first that fails decides; all passing crowns."""
    checks: list[Check] = []

    def passed(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(name, ok, detail))
        return ok

    def stop(decision: str, **numbers) -> Verdict:
        return Verdict(pr.number, decision, checks[-1].detail, checks, **numbers)

    if not passed('actor', not is_maintainer(pr.actor, pr.actor_writes, config), f'event by {pr.actor}'):
        return stop('ignore')
    if not passed('eligible', not (why := ineligible(pr, config)), why or f'opened by {pr.author}, open'):
        return stop('ignore')
    single = f'{pr.commits} commit(s)' + ('' if pr.commits == 1 else ': a push after opening; resubmit as a new PR')
    if not passed('one commit', pr.commits == 1, single):
        return stop('close')
    cla = any(CLA.fullmatch(line.strip()) for line in pr.body.splitlines())
    if not passed('cla', cla, 'CLA accepted' if cla else NO_CLA):
        return stop('close')
    if not passed('queue', not repo.earlier_open, f'earlier PRs first: {repo.earlier_open}'):
        return stop('wait')
    solver = solver_dir(pr.files, pr.author)
    scoped = solver is not None and solver not in repo.taken
    scope = solver if scoped else f'may change only solvers/{pr.author}/<unused n>/'
    if not passed('scope', scoped, f'{scope} and {ATTESTATION}'):
        return stop('close')
    try:
        att = Attestation.from_json(pr.attestation or '')
        genuine, detail = verify(att, config.dev_attestation_pubkey), f'signed by {att.signer}'
    except (ValueError, NotImplementedError) as e:
        att, genuine, detail = None, False, str(e)
    if not passed('signature', genuine, detail if genuine else f'attestation not genuine: {detail}') or att is None:
        return stop('close')
    if not passed('image', att.signer['kind'] == DEV or att.image == config.image, f'ran image {att.image}'):
        return stop('close')
    result = att.result
    age = chain.created_block - att.seed_block
    on_chain = chain.seed_block_hash is not None and chain.seed_block_hash == result.get('seed_block_hash')
    fresh = on_chain and 0 <= age <= config.freshness_blocks
    if not passed('seed', fresh, f'seed block {att.seed_block}, {age} blocks before the PR, its hash as on chain'):
        return stop('close')
    king_sha, sha = side(result, 'king').get('sha'), side(result, 'challenger').get('sha')
    stale = '' if king_sha == repo.king_sha else 'stale: re-run against the new KING and resubmit; '
    if not passed('king', not stale, f'{stale}ran against {king_sha}, KING {repo.king} is {repo.king_sha}'):
        return stop('close')
    if not passed('challenger', sha == pr.solver_sha, f'ran {sha}, the PR holds {pr.solver_sha}'):
        return stop('close')
    wanted = dict(
        challenge_id=config.challenge_id, module=config.module, tier=config.tier, n=config.seeds, margin=config.margin
    )
    ran = {key: result.get(key) for key in wanted}
    if not passed('config', ran == wanted, f'ran {ran}'):
        return stop('close')
    scores = [side(result, name).get('scores', []) for name in ('challenger', 'king')]
    valid = side(result, 'challenger').get('valid')
    whole = valid == config.seeds and all(is_scores(s, config.seeds) for s in scores)
    if not passed('validity', whole, f'{valid}/{config.seeds} challenger seeds valid'):
        return stop('close')
    challenger, king = (Entry('', [SeedResult(True, float(x), 0.0) for x in s]) for s in scores)
    bound = verdict(challenger, king, result['seed_block_hash'], config.margin)
    numbers = {'gain': bound['mean_gain'], 'lower_99': bound['lower_99']}
    crowned = result.get('crown') is True and bound['crown']
    if not passed('crown rule', crowned, f'lower_99 {bound["lower_99"]} against margin {config.margin}'):
        return stop('close', **numbers)
    gain = bound['mean_gain']
    calm = gain is not None and gain <= config.suspicious_gain
    if not passed('suspicious', calm, f'gain {gain} against suspicious_gain {config.suspicious_gain}'):
        return stop('needs_review', **numbers)
    passed('crown', True, f'{solver} takes the crown')
    return stop('crown', round=next_round(repo.leaderboard), **numbers)


def is_maintainer(login: str, writes: bool, config: Config) -> bool:
    return writes or login.lower() in {m.lower() for m in config.maintainers}


def ineligible(pr: PullRequest, config: Config) -> str | None:
    """Why the PR never enters the queue, if it doesn't."""
    if is_maintainer(pr.author, pr.author_writes, config):
        return f'opened by maintainer {pr.author}'
    if pr.state != 'open':
        return pr.state
    if pr.ever_closed:
        return 'reopened: a closed PR is never judged'
    return None


def solver_dir(files: list[str], author: str) -> str | None:
    """``solvers/<author>/<n>`` when the PR changes only that dir and ``attestation.json``."""
    rest = [path for path in files if path != ATTESTATION]
    roots = {'/'.join(path.split('/')[:3]) for path in rest}
    if ATTESTATION not in files or len(roots) != 1:
        return None
    root = roots.pop()
    inside = all(path.startswith(f'{root}/') for path in rest)
    return root if inside and re.fullmatch(rf'solvers/{re.escape(author)}/[1-9][0-9]*', root) else None


def side(result: dict, name: str) -> dict:
    return value if isinstance(value := result.get(name), dict) else {}


def is_scores(scores: Any, n: int) -> bool:
    return isinstance(scores, list) and len(scores) == n and all(type(x) in (int, float) for x in scores)


def next_round(leaderboard: str) -> int:
    """One past the highest round in ``LEADERBOARD.md`` (round 0 is the starting KING)."""
    return max(map(int, re.findall(r'^\|\s*(\d+)\s*\|', leaderboard, re.M)), default=0) + 1


def gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(['gh', *args], input=stdin, capture_output=True, text=True, check=True).stdout


def api(path: str, body: dict | None = None, method: str = 'GET') -> Any:
    stdin = None if body is None else json.dumps(body)
    return json.loads(gh('api', '-X', method, path, *(['--input', '-'] if body is not None else []), stdin=stdin))


def lines(path: str, jq: str) -> list[str]:
    return gh('api', '--paginate', path, '--jq', jq).splitlines()


def raw(repo: str, path: str, ref: str) -> str | None:
    """A file's contents at ``ref``, or ``None`` if there is none."""
    try:
        return gh('api', f'repos/{repo}/contents/{path}?ref={ref}', '-H', 'Accept: application/vnd.github.raw')
    except subprocess.CalledProcessError as e:
        if 'HTTP 404' in e.stderr:
            return None
        raise


def subdirs(repo: str, path: str, ref: str) -> dict[str, str]:
    """``{name: tree sha}`` of the directories in ``path`` at ``ref``."""
    try:
        entries = api(f'repos/{repo}/contents/{path}?ref={ref}')
    except subprocess.CalledProcessError as e:
        if 'HTTP 404' in e.stderr:
            return {}
        raise
    if not isinstance(entries, list):  # a file, not a directory
        return {}
    return {entry['name']: entry['sha'] for entry in entries if entry['type'] == 'dir'}


@cache
def writes(repo: str, login: str) -> bool:
    return gh('api', f'repos/{repo}/collaborators/{login}/permission', '--jq', '.permission').strip() in WRITERS


def pull_request(repo: str, number: int, actor: str | None = None) -> PullRequest:
    meta = api(f'repos/{repo}/pulls/{number}')
    author, head = meta['user']['login'], meta['head']['sha']
    files = lines(f'repos/{repo}/pulls/{number}/files', '.[].filename')
    solver = solver_dir(files, author)
    parent, name = solver.rsplit('/', 1) if solver else ('', '')
    return PullRequest(
        number=number,
        author=author,
        author_writes=writes(repo, author),
        actor=actor or author,
        actor_writes=writes(repo, actor or author),
        state='merged' if meta['merged'] else meta['state'],
        ever_closed='closed' in lines(f'repos/{repo}/issues/{number}/timeline', '.[].event'),
        commits=meta['commits'],
        files=files,
        created_at=datetime.fromisoformat(meta['created_at']),
        body=meta['body'] or '',
        labels=[label['name'] for label in meta['labels']],
        attestation=raw(repo, ATTESTATION, head),
        solver_sha=subdirs(repo, parent, head).get(name) if solver else None,
    )


def repo_facts(repo: str, pr: PullRequest, config: Config) -> Repo:
    king = (raw(repo, 'KING', MAIN) or '').strip()
    parent, name = king.rsplit('/', 1)
    open_prs = sorted(int(n) for n in lines(f'repos/{repo}/pulls?state=open&per_page=100', '.[].number'))
    return Repo(
        king=king,
        king_sha=subdirs(repo, parent, MAIN)[name],
        leaderboard=raw(repo, 'LEADERBOARD.md', MAIN) or '',
        taken=[f'solvers/{pr.author}/{n}' for n in subdirs(repo, f'solvers/{pr.author}', MAIN)],
        earlier_open=[n for n in open_prs if n < pr.number and not ineligible(pull_request(repo, n), config)],
    )


def chain_facts(subtensor: Any, pr: PullRequest) -> Chain:
    """The PR's creation block and the attested seed block's hash, from a ``bittensor.Subtensor``."""
    created = block_at(subtensor, pr.created_at)
    try:
        seed_block = Attestation.from_json(pr.attestation or '').seed_block
    except ValueError:
        return Chain(created, None)
    block_hash = subtensor.get_block_hash(seed_block) if 0 <= seed_block <= created else None
    return Chain(created, block_hash.lower().removeprefix('0x') if block_hash else None)


def block_at(subtensor: Any, when: datetime) -> int:
    """The last block stamped at or before ``when``."""
    block = subtensor.get_current_block()
    for _ in range(8):  # jump by the ~12 s block time, then walk the last steps
        step = round((subtensor.get_timestamp(block) - when).total_seconds() / BLOCK_SECONDS)
        if not step:
            break
        block -= step
    while subtensor.get_timestamp(block) > when:
        block -= 1
    while subtensor.get_timestamp(block + 1) <= when:
        block += 1
    return block


def apply(v: Verdict, name: str, pr: PullRequest, repo: Repo, config: Config) -> None:
    """Carry the verdict out; a rerun changes nothing more (a closed or merged PR is ignored from then on)."""
    number = str(pr.number)
    if v.decision == 'close':
        gh('pr', 'close', number, '--repo', name, '--comment', f'Closed by `gitt challenge verify`: {v.reason}')
    elif v.decision == 'needs_review' and NEEDS_REVIEW not in pr.labels:
        add_label(name, number, NEEDS_REVIEW)
        gh('pr', 'comment', number, '--repo', name, '--body', f'Held for a maintainer: {v.reason}')
    elif v.decision == 'crown':
        solver = solver_dir(pr.files, pr.author)
        add_label(name, number, config.crown_label.format(round=v.round))
        gh('pr', 'merge', number, '--repo', name, '--squash')
        gain, lower = (f'{x:+.2%}' if x is not None else '-' for x in (v.gain, v.lower_99))
        today = datetime.now(timezone.utc).date()
        row = f'| {v.round} | {solver} | {pr.author} | #{pr.number} | {gain} | {lower} | {today} |\n'
        files = {'KING': f'{solver}\n', 'LEADERBOARD.md': repo.leaderboard.rstrip('\n') + '\n' + row}
        commit_to_main(name, files, f'crown: round {v.round} goes to {solver} (#{pr.number})')


def add_label(name: str, number: str, label: str) -> None:
    gh('label', 'create', label, '--repo', name, '--force')
    gh('pr', 'edit', number, '--repo', name, '--add-label', label)


def commit_to_main(repo: str, files: dict[str, str], message: str) -> None:
    head = api(f'repos/{repo}/git/ref/heads/{MAIN}')['object']['sha']
    base = api(f'repos/{repo}/git/commits/{head}')['tree']['sha']
    blobs = [{'path': path, 'mode': '100644', 'type': 'blob', 'content': text} for path, text in files.items()]
    tree = api(f'repos/{repo}/git/trees', {'base_tree': base, 'tree': blobs}, 'POST')['sha']
    commit = api(f'repos/{repo}/git/commits', {'message': message, 'tree': tree, 'parents': [head]}, 'POST')['sha']
    api(f'repos/{repo}/git/refs/heads/{MAIN}', {'sha': commit}, 'PATCH')


@click.command('verify')
@click.option('--repo', 'name', required=True, metavar='OWNER/NAME', help='The challenge repo.')
@click.option('--pr', 'number', type=int, required=True, help='The pull request.')
@click.option('--actor', help="Who triggered the event (default: the PR's author).")
@click.option('--network', default='archive', show_default=True, help="Subtensor network (old blocks' timestamps).")
@click.option('--apply', 'act', is_flag=True, help='Carry the verdict out with gh: label, merge, comment, close.')
def verify_command(name, number, actor, network, act):
    """Judge a challenge PR by its attestation alone and print the verdict JSON.

    [dim]Decisions: crown, close, wait (an earlier PR goes first), needs_review (a human looks) or ignore (a
    maintainer's event, or a PR that never enters the queue). Reads only, unless --apply. Never runs submitted code.[/dim]
    """
    import bittensor as bt

    try:
        config = Config.from_json(raw(name, '.gittensor/challenge.json', MAIN) or '')
        pr = pull_request(name, number, actor)
        repo = repo_facts(name, pr, config)
        v = decide(pr, repo, chain_facts(bt.Subtensor(network=network), pr), config)
        if act:
            apply(v, name, pr, repo, config)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f'{" ".join(e.cmd)}: {e.stderr.strip()}') from e
    click.echo(v.to_json())
