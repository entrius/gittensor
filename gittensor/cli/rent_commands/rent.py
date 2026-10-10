# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``gitt rent``: a whole GPU box as a pod, from the terminal, the way ``lium`` does it (vault 29 §6).

    gitt rent login <key>          save the API key (or export GITTENSOR_API_KEY)
    gitt rent ls                   the boxes for rent, one row each: uid, size, $/hr, host, where, status
    gitt rent up --box 45 [-n dev] rent that box: order, wait for active, print the ssh line
    gitt rent up RTX5090 -c 1      any free box of a type and size (the older form, through /offers)
    gitt rent ssh [dev] [-- cmd]   ssh in (one open rental needs no name)
    gitt rent ps                   your rentals
    gitt rent extend [dev] 2       add hours
    gitt rent rm [dev]             stop, wait for ended, print what it cost
    gitt rent balance

The CLI does the waiting and the polling; the API stays plain. ``--json`` on any command prints the API's objects."""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

import click
from rich.table import Table

from gittensor.cli.help import StyledGroup
from gittensor.cli.helpers import console, err_console
from gittensor.cli.json_output import emit_error_json, emit_json
from gittensor.cli.rent_commands.api import (
    OPEN_STATES,
    ApiError,
    RentApi,
    RentConfig,
    find_box,
    find_offer,
    resolve,
    ssh_public_keys,
)

POLL_S = 2
MIN_HOURS, MAX_HOURS = (
    0.25,
    168,
)  # the API's (shared/types RENTAL_MIN_MINUTES, RENTAL_MAX_HOURS; the controller's cfg.RENTAL_MAX_HOURS).0
WAIT_ACTIVE_S = 15 * 60  # a cold pull of a big image; ends_at only starts at active (29 §1 #11)
WAIT_ENDED_S = 5 * 60
SSH_OPTS = ('-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR')
# every pod has a fresh host key on a reused host:port, so pinning across rentals would only ever refuse

# the failure reasons as a customer should read them (the controller's codes, 29 §3)
REASONS = {
    'no_box_fits': 'no box of that type and size was free in time (nothing billed); `gitt rent ls` and try again',
    'box_busy': 'that box was taken; pick another (`gitt rent ls`); nothing billed',
    'pull_failed': 'the box could not pull the image (nothing billed): check the name and tag are public '
    '(`docker pull` it yourself to see), or use a quick-pick',
    'start_failed': 'the pod started but never answered on :22 (nothing billed): the image has to run sshd '
    '(the quick-picks do); needing docker inside? use the dind quick-pick',
    'box_lost': "the miner's box went dark while you were on it; you were billed only up to its last heartbeat",
    'ends_at': 'the rental reached its end time',
    'customer_stop': 'stopped by you',
    'balance': 'stopped: the balance ran out (`gitt rent balance`)',
}


def _json_flag(f):
    return click.option('--json', 'json_mode', is_flag=True, help='print the API objects as JSON')(f)


def _fail(e: ApiError, json_mode: bool) -> None:
    if json_mode:
        emit_error_json(e.message, e.kind, status=e.status)
    else:
        err_console.print(f'[red]Error:[/red] {e.message}')
    sys.exit(2 if e.kind in ('no_rental', 'ambiguous', 'usage', 'invalid_request') else 1)


def _api(cfg: RentConfig) -> RentApi:
    if not cfg.key:
        raise ApiError(
            'not logged in: `gitt rent login <api key>` or export GITTENSOR_API_KEY (keys: the app, /keys)', 'usage'
        )
    return RentApi(cfg.url, cfg.key)


def _usd(cents: int | float | None) -> str:
    return f'${(cents or 0) / 100:.2f}'


def _ssh_line(r: dict) -> str:
    port = (r.get('port_map') or {}).get('22')
    return f'ssh root@{r.get("host")} -p {port}' if r.get('host') and port else (r.get('ssh') or '')


def _reason(r: dict) -> str:
    code = r.get('reason') or ''
    return REASONS.get(code, code)


def _label(cfg: RentConfig, r: dict) -> str:
    name = cfg.name_of(r['id'])
    return f'{name} ({r["id"]})' if name else r['id']


def _wait(api: RentApi, first: dict, until: tuple[str, ...], limit_s: float, quiet: bool) -> dict:
    """Poll a rental from the reply ``first`` until its state is one of ``until`` (or a terminal one), with a live
    line on stderr per state."""
    started = time.monotonic()
    rental_id, last, r = first['id'], '', first
    while True:
        state = r.get('state', '')
        if state != last and not quiet:
            err_console.print(f'  {state:<10} {time.monotonic() - started:4.0f} s', highlight=False)
            last = state
        if state in until or state in ('ended', 'failed'):
            return r
        if time.monotonic() - started > limit_s:
            raise ApiError(
                f'{rental_id} is still {state} after {limit_s / 60:.0f} min; `gitt rent ps` later', 'timeout'
            )
        time.sleep(POLL_S)
        r = api.rental(rental_id)


@click.group(name='rent', cls=StyledGroup)
def rent_group():
    """Rent a whole GPU box as a pod and ssh in: `ls`, `up`, `ssh`, `rm`. Keys: the app, /keys."""


@rent_group.command('login')
@click.argument('key')
@click.option('--url', default=None, help='the product API (default: the saved one, else the testnet product)')
def login_command(key, url):
    """Save the API key (from the app, /keys) and, optionally, which product to talk to."""
    cfg = RentConfig.load()
    cfg.key = key.strip()
    if url:
        cfg.url = url.rstrip('/')
    try:
        bal = _api(cfg).balance()
    except ApiError as e:
        _fail(e, False)
        return
    cfg.save()
    console.print(f'Logged in to {cfg.url}: balance {_usd(bal.get("balance_cents"))}.')


def _box_cell(box: dict, key: str, fmt: str = '{:.0f}') -> str:
    value = (box.get('host') or {}).get(key)
    return fmt.format(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else '—'


def _status_cell(box: dict) -> str:
    status = str(box.get('status') or '')
    if status == 'available':
        return '[green]free[/green]'
    if status == 'rented':
        until = box.get('rented_until')
        return f'[yellow]rented[/yellow] until {_when(until)}' if until else '[yellow]rented[/yellow]'
    return f'[dim]{status or "—"}[/dim]'


@rent_group.command('ls')
@click.option('--type', 'gpu_type', default=None, help='only this GPU type (e.g. RTX5090)')
@click.option('--count', 'count', type=int, default=None, help='only boxes of this size (1, 2, 4, 8)')
@click.option('--country', default=None, help='only boxes in this country (ISO 3166-1 alpha-2, e.g. US)')
@click.option('--all', 'show_all', is_flag=True, help='rented and unavailable boxes too')
@_json_flag
def ls_command(gpu_type, count, country, show_all, json_mode):
    """The boxes for rent, one row each, best download first: `gitt rent up --box <uid>` rents one of them."""
    cfg = RentConfig.load()
    try:
        api = _api(cfg)
        boxes, bal = api.boxes(), api.balance()
    except ApiError as e:
        _fail(e, json_mode)
        return
    if json_mode:
        emit_json({**boxes, 'balance': bal})
        return
    cents = bal.get('balance_cents') or 0
    rows = []
    for b in boxes.get('boxes') or []:
        if not show_all and b.get('status') != 'available':
            continue
        if gpu_type and str(b.get('gpu_type', '')).lower() != gpu_type.lower():
            continue
        if count is not None and int(b.get('gpu_count') or 0) != count:
            continue
        where = ((b.get('host') or {}).get('location') or {}).get('country') or ''
        if country and str(where).upper() != country.strip().upper():
            continue
        rows.append((b, where))
    table = Table(title='boxes for rent', show_lines=False)
    for col in ('#', 'Box', '$/hr', 'CPU', 'RAM', '↓Mbps', '↑Mbps', 'Where', 'Status', 'Runway', 'Hotkey'):
        table.add_column(col, justify='left' if col in ('Box', 'Where', 'Status', 'Hotkey') else 'right')
    for b, where in rows:
        per_hr = float(b.get('usd_per_hr') or 0)
        runway = f'{cents / 100 / per_hr:.1f} h' if per_hr and cents > 0 else '—'
        uid = b.get('uid')
        table.add_row(
            str(uid) if uid is not None else '—',
            f'{b.get("gpu_count")}× {b.get("gpu_type")}',
            f'{per_hr:.2f}',
            _box_cell(b, 'cpu_threads'),
            _box_cell(b, 'ram_gb'),
            _box_cell(b, 'down_mbps'),
            _box_cell(b, 'up_mbps'),
            str(where or '—'),
            _status_cell(b),
            runway,
            f'[dim]{str(b.get("hotkey", ""))[:10]}…[/dim]',
        )
    if rows:
        console.print(table)
        console.print(
            '[dim]CPU threads, RAM GB, download / upload Mbps as the pool measured the box; '
            '`gitt rent up --box <#|hotkey>` rents one, `gitt rent ls --json` has every field and the full hotkey[/dim]'
        )
    elif show_all or gpu_type or count is not None or country:
        console.print('No box matches; `gitt rent ls --all` lists every box, rented ones too.')
    else:
        console.print('Nothing is free right now; `gitt rent ls --all` lists the rented boxes too.')
    for vendor, image in default_images(boxes).items():
        console.print(f'default image ({vendor}): {image or "none published; give one with --image"}')
    fleet = boxes.get('fleet')
    console.print(f'balance {_usd(cents)}' + (f' · fleet {fleet}' if fleet and fleet != 'ok' else ''))


def offer_vendor(offers: dict, gpu_type: str) -> str:
    """The vendor the API names on the offer row or the box (``nvidia`` when it names none: the whole catalog until AMD)."""
    for o in (offers.get('offers') or []) + (offers.get('boxes') or []):
        if str(o.get('gpu_type', '')).lower() == gpu_type.lower():
            return str(o.get('vendor') or 'nvidia')
    return 'nvidia'


def default_images(offers: dict) -> dict[str, str]:
    """The first quick-pick per vendor on offer: a CUDA image does not run on an AMD box, so the default follows the
    box's vendor. A vendor with offers and no image maps to '' (the customer must name one)."""
    rows = (offers.get('offers') or []) + (offers.get('boxes') or [])  # the offer rows, or the box listing's
    vendors = {str(o.get('vendor') or 'nvidia') for o in rows} or {'nvidia'}
    out: dict[str, str] = {}
    for vendor in sorted(vendors):
        out[vendor] = ''
        for i in offers.get('images') or []:
            if str(i.get('vendor') or 'nvidia') == vendor and i.get('image'):
                out[vendor] = str(i['image'])
                break
    return out


@rent_group.command('up')
@click.argument('gpu_type', required=False)
@click.option('-c', '--count', default=1, show_default=True, help='GPUs in the box (1, 2, 4, 8)')
@click.option('-H', '--hours', default=1.0, show_default=True, help='0.25 to 168; billed per minute once active')
@click.option('-i', '--image', default=None, help='any public image that runs sshd (default: the first quick-pick)')
@click.option('-p', '--port', 'ports', multiple=True, type=int, help='a container port to publish (22 is always)')
@click.option('-e', '--env', 'envs', multiple=True, help='KEY=VALUE in the pod')
@click.option(
    '-k',
    '--key',
    'key_paths',
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help='a public key file (default: every ~/.ssh/id_*.pub)',
)
@click.option('-n', '--name', default=None, help='a local name for `ssh`, `extend`, `rm`')
@click.option(
    '--box', 'box_ref', default=None, help='rent this one box: its # from `ls`, its hotkey, or a unique prefix'
)
@click.option('--country', default=None, help='place only on a box in this country (ISO 3166-1 alpha-2, e.g. US)')
@click.option('--queue', is_flag=True, help='order even when nothing is free now (waits up to the placement grace)')
@click.option('--no-wait', is_flag=True, help='print the id and return; `gitt rent ps` to follow')
@_json_flag
def up_command(
    gpu_type, count, hours, image, ports, envs, key_paths, name, box_ref, country, queue, no_wait, json_mode
):
    """Rent a box: order, wait until it is active, print the ssh line.

    `--box` names one box from `gitt rent ls` (the type and size follow from it); a type alone takes any free box
    of that type and size. A failed start is retried once. Refuses up front when the box, or every box of that
    size, is taken (``--queue`` to wait anyway).
    """
    cfg = RentConfig.load()
    try:
        api = _api(cfg)
        if not gpu_type and not box_ref:
            raise ApiError(
                'say which: `gitt rent up --box <#>` (from `gitt rent ls`) or `gitt rent up <gpu type>`', 'usage'
            )
        _check_hours(hours)
        keys = ssh_public_keys(list(key_paths))
        if not keys:
            raise ApiError('no SSH public key: none under ~/.ssh/id_*.pub; give one with --key', 'usage')
        env = {}
        for kv in envs:
            k, sep, v = kv.partition('=')
            if not sep or not k:
                raise ApiError(f'--env {kv!r}: KEY=VALUE', 'usage')
            env[k] = v
        if name and name in cfg.names:
            held = next(
                (x for x in api.rentals() if x.get('id') == cfg.names[name] and x.get('state') in OPEN_STATES), None
            )
            if held:
                raise ApiError(
                    f'{name!r} is {held["state"]} ({held["id"]}): pick another name or `gitt rent rm {name}`', 'usage'
                )
        pin: dict = {}
        if box_ref:
            offers = api.boxes()
            pinned = find_box(offers, box_ref.strip())
            if pinned is None:
                raise ApiError(f'no box {box_ref!r} in the listing (`gitt rent ls --all`: # or hotkey)', 'usage')
            if gpu_type and str(pinned.get('gpu_type', '')).lower() != gpu_type.lower():
                raise ApiError(f'box {box_ref} is a {pinned.get("gpu_type")}, not a {gpu_type}', 'usage')
            gpu_type, count = str(pinned['gpu_type']), int(pinned['gpu_count'])
            if pinned.get('status') != 'available' and not queue:
                until = pinned.get('rented_until')
                raise ApiError(
                    f'box {box_ref} is {pinned.get("status")}'
                    + (f' until {_when(until)}' if until else '')
                    + ' (`gitt rent ls` for a free one; --queue to wait for this one)',
                    'no_box',
                )
            pin = {'box_hotkey': str(pinned['hotkey'])}
        else:
            offers = api.offers()
            found = find_offer(offers, gpu_type, count)
            if found is None:
                raise ApiError(f'no such box size: {gpu_type} ×{count} (`gitt rent ls --all`)', 'usage')
            gpu_type, box = found
            if not int(box.get('available') or 0) and not queue:
                raise ApiError(f'no {gpu_type} ×{count} box is free right now (`gitt rent ls`; --queue to wait)', 'no_box')  # fmt: skip
        if image is None:
            vendor = offer_vendor(offers, gpu_type)
            image = default_images(offers).get(vendor)
            if not image:
                raise ApiError(
                    f'no quick-pick image for {gpu_type} ({vendor}) yet; give one that runs sshd on :22 with --image',
                    'usage',
                )
        body = {
            **pin,
            'gpu_type': gpu_type,
            'gpu_count': count,
            'hours': hours,
            'image': image,
            'ssh_pubkeys': keys,
            'ports': sorted({22, *ports}),
        }
        if env:
            body['env'] = env
        if country:
            code = country.strip().upper()
            if len(code) != 2 or not code.isalpha():
                raise ApiError(f'--country {country!r}: a two-letter country code, e.g. US (`gitt rent ls` Where)', 'usage')  # fmt: skip
            body['country'] = code
        r = _order_and_wait(api, cfg, body, name, no_wait, json_mode)
    except ApiError as e:
        _fail(e, json_mode)
        return
    if json_mode:
        emit_json(r)
        sys.exit(1 if r.get('state') == 'failed' else 0)
    if r.get('state') == 'active':
        console.print(
            f'[green]active[/green] {_label(cfg, r)} · {r["gpu_type"]} ×{r["gpu_count"]} · '
            f'{_usd(r.get("usd_cents_per_hr"))}/hr until {_when(r.get("ends_at"))}'
        )
        console.print(f'  {_ssh_line(r)}')
        extra = {k: v for k, v in (r.get('port_map') or {}).items() if k != '22'}
        if extra:
            console.print('  ports: ' + ', '.join(f'{k} → {r["host"]}:{v}' for k, v in extra.items()))
    elif no_wait:
        console.print(f'{r["state"]} {_label(cfg, r)} · `gitt rent ps` to follow')
    else:
        console.print(f'[red]{r["state"]}[/red] {_label(cfg, r)}: {_reason(r)}')
        sys.exit(1)


def _check_hours(hours: float) -> None:
    # the API's limits (vault 29 §2), checked before anything else so a typo is not answered with "nothing is free"
    if not MIN_HOURS <= hours <= MAX_HOURS:
        raise ApiError(f'hours must be a number from {MIN_HOURS} to {MAX_HOURS} (-H 0.5 is 30 minutes)', 'usage')


def _order_and_wait(api: RentApi, cfg: RentConfig, body: dict, name: str | None, no_wait: bool, quiet: bool) -> dict:
    retried = False
    while True:
        r = api.order(body)
        if name:
            cfg.names[name] = r['id']  # a name from an earlier, finished rental is simply taken over
            cfg.save()
        if no_wait:
            return r
        if not quiet:
            err_console.print(f'ordered {r["id"]} ({body["gpu_type"]} ×{body["gpu_count"]}, {body["image"]})')
        r = _wait(api, r, ('active',), WAIT_ACTIVE_S, quiet)
        if r.get('state') == 'failed' and r.get('reason') == 'start_failed' and not retried:
            retried = True
            if not quiet:
                err_console.print('  start failed once; ordering again')
            continue
        return r


def _when(ts) -> str:
    if not ts:
        return '?'
    fmt = (
        '%H:%M UTC'
        if time.strftime('%Y-%m-%d', time.gmtime(ts)) == time.strftime('%Y-%m-%d', time.gmtime())
        else '%m-%d %H:%M UTC'
    )
    return time.strftime(fmt, time.gmtime(ts))


@rent_group.command('ps')
@click.option('--all', 'show_all', is_flag=True, help='ended and failed rentals too')
@_json_flag
def ps_command(show_all, json_mode):
    """Your rentals: open ones by default."""
    cfg = RentConfig.load()
    try:
        rentals = _api(cfg).rentals()
    except ApiError as e:
        _fail(e, json_mode)
        return
    rows = [r for r in rentals if show_all or r.get('state') in OPEN_STATES]
    if json_mode:
        emit_json({'rentals': rows})
        return
    if not rows:
        console.print('No open rentals.' + ('' if show_all else ' `gitt rent ps --all` for past ones.'))
        return
    # ids and ssh lines fold rather than truncate (a cut id cannot be pasted back); past rentals say when and why
    table = Table(show_lines=False)
    table.add_column('Name')
    table.add_column('Id', overflow='fold')
    table.add_column('State', min_width=9)
    table.add_column('Box')
    table.add_column('SSH', overflow='fold')
    table.add_column('Created')
    table.add_column('Ends / ended' if show_all else 'Ends')
    table.add_column('Billed', justify='right')
    if show_all:
        table.add_column('Why')
    for r in rows:
        state = r.get('state', '')
        colour = {'active': 'green', 'failed': 'red', 'ended': 'dim'}.get(state, 'yellow')
        when = r.get('ends_at') if state in OPEN_STATES else r.get('ended_at')
        table.add_row(
            cfg.name_of(r['id']) or '—',
            r['id'],
            f'[{colour}]{state}[/{colour}]',
            f'{r.get("gpu_type")} ×{r.get("gpu_count")}',
            _ssh_line(r) if state == 'active' else '—',
            _when(r.get('created_at')),
            _when(when) if when else '—',
            _usd(r.get('billed_cents')),
            *(((_reason(r) if state in ('ended', 'failed') else ''),) if show_all else ()),
        )
    console.print(table)


@rent_group.command('ssh', context_settings={'ignore_unknown_options': True, 'allow_interspersed_args': False})
@click.argument('args', nargs=-1, type=click.UNPROCESSED)
def ssh_command(args):
    """ssh into a rental: `gitt rent ssh dev`, or `gitt rent ssh dev -- nvidia-smi -L` for one command.

    With one open rental the name may be left out: `gitt rent ssh`, `gitt rent ssh -- nvidia-smi -L`.
    """
    # click drops a leading `--` but keeps a later one, so: `name -- cmd` arrives with the separator (the name must
    # then resolve: a typo never runs as a command on the box), `-- cmd` and `name` arrive without it.
    args = list(args)
    if '--' in args:
        names, command = args[: args.index('--')], args[args.index('--') + 1 :]
        if len(names) != 1:
            _fail(ApiError('usage: gitt rent ssh [NAME] [-- COMMAND...]', 'usage'), False)
            return
        ref = names[0]
    elif len(args) <= 1:
        ref, command = (args[0] if args else None), []
    else:
        ref, command = None, args
    cfg = RentConfig.load()
    try:
        api = _api(cfg)
        r = resolve(cfg, api.rentals(), ref)
        if r.get('state') != 'active':
            raise ApiError(
                f'{_label(cfg, r)} is {r.get("state")}, not active' + (f': {_reason(r)}' if r.get('reason') else ''),
                'usage',
            )
        port = (r.get('port_map') or {}).get('22')
        if not r.get('host') or not port:
            raise ApiError(f'{_label(cfg, r)} has no ssh endpoint yet', 'usage')
    except ApiError as e:
        _fail(e, False)
        return
    argv = ['ssh', *SSH_OPTS, '-p', str(port), f'root@{r["host"]}', *command]
    if not command:  # interactive: show the line to reuse by hand; a scripted command stays quiet
        err_console.print(f'[dim]{shlex.join(argv)}[/dim]', highlight=False)
    sys.stdout.flush()
    os.execvp('ssh', argv)


@rent_group.command('extend', context_settings={'ignore_unknown_options': True})  # so `-0.25` is hours, not an option
@click.argument('ref', required=False)
@click.argument('hours', type=float)
@_json_flag
def extend_command(ref, hours, json_mode):
    """Add hours to a rental (0.25 to 168; the end may be at most 7 days from now)."""
    cfg = RentConfig.load()
    try:
        _check_hours(hours)
        api = _api(cfg)
        r = resolve(cfg, api.rentals(), ref)
        r = api.extend(r['id'], hours)
    except ApiError as e:
        _fail(e, json_mode)
        return
    if json_mode:
        emit_json(r)
    else:
        console.print(f'{_label(cfg, r)} now ends at {_when(r.get("ends_at"))}')


@rent_group.command('rm')
@click.argument('ref', required=False)
@click.option('--no-wait', is_flag=True, help='ask for the stop and return')
@_json_flag
def rm_command(ref, no_wait, json_mode):
    """Stop a rental, wait until it is ended, and say what it cost."""
    cfg = RentConfig.load()
    try:
        api = _api(cfg)
        r = resolve(cfg, api.rentals(), ref)
        rid = r['id']
        r = api.stop(rid)
        if not no_wait:
            r = _wait(api, r, ('ended', 'failed'), WAIT_ENDED_S, json_mode)
        bal = api.balance() if not no_wait else None
    except ApiError as e:
        _fail(e, json_mode)
        return
    if json_mode:
        emit_json({'rental': r, 'balance': bal})
        return
    if no_wait:
        console.print(f'{_label(cfg, r)} {r.get("state")} · `gitt rent ps` to follow')
        return
    used = (r.get('ended_at') or 0) - (r.get('started_at') or 0) if r.get('started_at') else 0
    # the API bills every minute begun and never fewer than the minimum: say so when that is what the bill is
    minimum = f' (the {MIN_HOURS * 60:.0f}-minute minimum)' if 0 < used < MIN_HOURS * 3600 else ''
    console.print(
        f'{r.get("state")} {_label(cfg, r)} · used {used / 60:.0f} min · billed {_usd(r.get("billed_cents"))}{minimum}'
        f' · balance {_usd((bal or {}).get("balance_cents"))}'
    )


@rent_group.command('balance')
@_json_flag
def balance_command(json_mode):
    """Balance, burn rate, and the recent ledger."""
    cfg = RentConfig.load()
    try:
        bal = _api(cfg).balance()
    except ApiError as e:
        _fail(e, json_mode)
        return
    if json_mode:
        emit_json(bal)
        return
    burn = bal.get('burn_cents_per_hr') or 0
    line = f'balance {_usd(bal.get("balance_cents"))}'
    if burn:
        line += f' · burning {_usd(burn)}/hr ({(bal.get("balance_cents") or 0) / burn:.1f} h left)'
    console.print(line)
    for e in (bal.get('entries') or [])[:10]:
        console.print(
            f'  {_when(e.get("created_at"))}  {e.get("kind", ""):<8} {_usd(e.get("cents")):>9}  {e.get("ref", "")}'
        )
