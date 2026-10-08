# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt down — a clean leave: stop taking rentals, wait for the customer who has the box, then remove the agent and its
runner and drain and remove our workloads. Pay is held back 48 h (#1818), so a miner must be able to leave without
forfeiting it: the only thing that costs standing is a customer's pod dying under them, and this waits for it."""

from __future__ import annotations

import time
from typing import Any

import click

from gittensor.agent.config import INSTANCE_LABEL, RENTAL_LABEL
from gittensor.agent.launch import (
    Workload,
    down_commands,
    drain_mark_command,
    parse_workloads,
    render,
    workload_list_command,
)
from gittensor.cli.helpers import console, err_console
from gittensor.cli.json_output import emit_json

from . import docker_exec

WAIT_POLL_S = 30.0


def list_workloads() -> tuple[list[Workload], list[Workload], str]:
    """The controller's workload containers, customers' pods on this box, and '' or why they could not be listed."""
    found: list[list[Workload]] = []
    for label in (INSTANCE_LABEL, RENTAL_LABEL):
        proc = docker_exec.run_docker(workload_list_command(label))
        if proc.returncode != 0:
            return [], [], (proc.stderr or proc.stdout).strip()[:200]
        found.append(parse_workloads(proc.stdout))
    return found[0], found[1], ''


def running_pods() -> list[Workload]:
    """Customers' pods still running on this box (a listing that fails counts as none: the leave goes on)."""
    proc = docker_exec.run_docker(workload_list_command(RENTAL_LABEL))
    return [w for w in parse_workloads(proc.stdout) if w.running] if proc.returncode == 0 else []


def _until(ends_at: float | None) -> str:
    if ends_at is None:
        return 'for up to 7 days'
    left = max(0.0, ends_at - time.time())
    when = time.strftime('%m-%d %H:%M UTC', time.gmtime(ends_at))
    return f'until {when} ({left / 3600:.1f} h)' if left >= 3600 else f'until {when} ({left / 60:.0f} min)'


def wait_for_customers(pods: list[Workload], poll_s: float = WAIT_POLL_S) -> bool:
    """Wait until no customer's pod runs here. False when the miner interrupted the wait."""
    for p in pods:
        err_console.print(f'[yellow]a customer has this box {_until(p.ends_at)}[/yellow] ({p.name})')
    err_console.print(
        '[dim]no new rental will be placed here; waiting for the customer to finish. Ctrl-C leaves the box up and '
        'waiting (`gitt down` again later); `gitt down --now` ends the rental and costs the box its standing.[/dim]'
    )
    try:
        while pods:
            time.sleep(poll_s)
            pods = running_pods()
    except KeyboardInterrupt:
        return False
    return True


@click.command('down')
@click.option(
    '--now', is_flag=True, default=False, help="Skip the wait: end a customer's rental and kill our workloads at once."
)
@click.option('--dry-run', is_flag=True, default=False, help='Print the docker command(s) without running them.')
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def down_command(now, dry_run, json_mode):
    """Leave cleanly: stop taking rentals, wait for a customer who has the box, then remove the runner that keeps the
    agent updated and the agent itself, and drain and remove every workload the controller placed here.

    \b
    Pay is held back 48 h; it keeps arriving after you leave as long as the hotkey stays registered that long, and
    leaving this way never forfeits it. The agent goes before our workloads so the controller only ever sees this box
    as unreachable, never a workload vanishing under a live agent. The sshd host-key volume stays, so a later
    `gitt up` keeps the same host key. --now skips the wait and ends a customer's rental (the box loses standing).
    """
    instances, pods, list_error = list_workloads()
    workloads = instances + pods
    pods = [p for p in pods if p.running]
    plan = down_commands(workloads=workloads, now=now)
    if dry_run:
        if json_mode:
            emit_json(
                {
                    'success': True,
                    'dry_run': True,
                    'workloads': [w.name for w in workloads],
                    'list_error': list_error,
                    'would_wait_for': [p.name for p in pods] if not now else [],
                    'commands': [render(drain_mark_command())] * (not now) + [render(c) for c in plan],
                }
            )
        else:
            if list_error:
                err_console.print(
                    f'[yellow]could not list our workloads ({list_error}); removing the agent only[/yellow]'
                )
            console.print('[bold]Would run:[/bold]')
            if not now:
                click.echo(f'  {render(drain_mark_command())}')
                for p in pods:
                    click.echo(f'  (wait for {p.name}, {_until(p.ends_at)})')
            for cmd in plan:
                click.echo(f'  {render(cmd)}')
        return

    if not now and not list_error:
        docker_exec.run_docker(drain_mark_command())  # no agent running: nothing to mark, nothing to wait for
        if pods and not wait_for_customers(pods):
            if json_mode:
                emit_json({'success': False, 'waiting_for': [p.name for p in pods], 'detail': 'interrupted'})
            else:
                err_console.print('[yellow]left the box up and off the market; run `gitt down` again later[/yellow]')
            raise SystemExit(1)
        instances, pods, list_error = list_workloads()  # the pod is gone: the plan no longer drains it
        workloads = instances + pods
        plan = down_commands(workloads=workloads, now=now)

    names = {w.container_id: w.name for w in workloads}
    outcome = []
    for cmd in plan:
        proc = docker_exec.run_docker(cmd)
        target = cmd[-1]
        row: dict[str, Any] = {'container': names.get(target, target), 'action': cmd[1]}
        if proc.returncode == 0:
            row['ok'] = True
        elif 'No such container' in (proc.stderr or ''):
            row.update(ok=False, detail='not running')
        else:
            row.update(ok=False, detail=(proc.stderr or proc.stdout).strip()[:200])
        outcome.append(row)
    if json_mode:
        emit_json(
            {
                'success': True,
                'dry_run': False,
                'workloads': [w.name for w in workloads],
                'list_error': list_error,
                'containers': outcome,
            }
        )
        return
    if list_error:
        err_console.print(f'[yellow]could not list our workloads ({list_error}); removed the agent only[/yellow]')
    for row in outcome:
        verb = {'stop': 'Drained', 'rm': 'Removed'}.get(row['action'], row['action'])
        if row['ok']:
            err_console.print(f'[green]{verb}[/green] {row["container"]}')
        else:
            err_console.print(f'[dim]{row["container"]}: {row["detail"]}[/dim]')
    err_console.print(
        '[dim]pay already accrued arrives over the next 48 h: keep the hotkey registered until then.[/dim]'
    )
