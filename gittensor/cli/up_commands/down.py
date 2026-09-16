# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt down — a clean leave: the agent and its runner go first, then our workloads are drained and stopped."""

from __future__ import annotations

from typing import Any

import click

from gittensor.agent.launch import Workload, down_commands, parse_workloads, render, workload_list_command
from gittensor.cli.helpers import console, err_console
from gittensor.cli.json_output import emit_json

from . import docker_exec


def list_workloads() -> tuple[list[Workload], str]:
    """The controller's workload containers on this box, and '' or why they could not be listed."""
    proc = docker_exec.run_docker(workload_list_command())
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout).strip()[:200]
    return parse_workloads(proc.stdout), ''


@click.command('down')
@click.option('--now', is_flag=True, default=False, help='Skip the drain: kill our workloads instead of waiting.')
@click.option('--dry-run', is_flag=True, default=False, help='Print the docker command(s) without running them.')
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def down_command(now, dry_run, json_mode):
    """Leave cleanly: stop the runner and the agent first, then drain and stop every workload the controller placed
    here (gt-i-*: SIGTERM, wait up to the manifest's drain.max_s, then remove). The agent goes first so the
    controller sees the box as unreachable and then its container as stopped, never as killed under a live agent.

    \b
    The sshd host-key volume stays, so a later `gitt up` keeps the same host key and needs no re-registration.
    --now skips the drain wait.
    """
    workloads, list_error = list_workloads()
    plan = down_commands(workloads=workloads, now=now)
    if dry_run:
        if json_mode:
            emit_json(
                {
                    'success': True,
                    'dry_run': True,
                    'workloads': [w.name for w in workloads],
                    'list_error': list_error,
                    'commands': [render(c) for c in plan],
                }
            )
        else:
            if list_error:
                err_console.print(
                    f'[yellow]could not list our workloads ({list_error}); removing the agent only[/yellow]'
                )
            console.print('[bold]Would run:[/bold]')
            for cmd in plan:
                click.echo(f'  {render(cmd)}')
        return

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
