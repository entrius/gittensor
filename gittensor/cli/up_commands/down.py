# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt down — stop the compute agent and its runner."""

from __future__ import annotations

import click

from gittensor.agent.launch import down_commands, render
from gittensor.cli.helpers import console, err_console
from gittensor.cli.json_output import emit_json

from . import docker_exec


@click.command('down')
@click.option('--dry-run', is_flag=True, default=False, help='Print the docker command(s) without running them.')
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def down_command(dry_run, json_mode):
    """Stop the compute agent (and the runner that keeps it updated).

    Removes the containers only; the sshd host-key volume stays so a later `gitt up` keeps the same host key.
    """
    plan = down_commands()
    if dry_run:
        if json_mode:
            emit_json({'success': True, 'dry_run': True, 'commands': [render(c) for c in plan]})
        else:
            console.print('[bold]Would run:[/bold]')
            for cmd in plan:
                click.echo(f'  {render(cmd)}')
        return

    outcome = []
    for cmd in plan:
        proc = docker_exec.run_docker(cmd)
        name = cmd[-1]
        if proc.returncode == 0:
            outcome.append({'container': name, 'removed': True})
        elif 'No such container' in (proc.stderr or ''):
            outcome.append({'container': name, 'removed': False, 'detail': 'not running'})
        else:
            outcome.append({'container': name, 'removed': False, 'detail': (proc.stderr or proc.stdout).strip()[:200]})

    if json_mode:
        emit_json({'success': True, 'dry_run': False, 'containers': outcome})
        return
    for row in outcome:
        if row['removed']:
            err_console.print(f'[green]Removed[/green] {row["container"]}')
        else:
            err_console.print(f'[dim]{row["container"]}: {row["detail"]}[/dim]')
