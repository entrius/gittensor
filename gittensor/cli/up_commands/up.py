# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt up — check prerequisites, then start the compute agent. The miner does nothing after this."""

from __future__ import annotations

import sys

import click

from gittensor.agent.config import (
    AGENT_CONTAINER_NAME,
    AGENT_HTTP_PORT,
    AGENT_IMAGE,
    AGENT_SSH_PORT,
    RUNNER_CONTAINER_NAME,
    RUNNER_IMAGE,
)
from gittensor.agent.launch import agent_run_command, render, runner_run_command
from gittensor.cli.helpers import NETWORK_CHOICE, console, err_console
from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.helpers import NETUID_DEFAULT, _error, _load_config_value, _resolve_endpoint

from . import docker_exec
from .prereqs import HostProbe, PrereqReport, render_table, run_prereqs


def _make_probe() -> HostProbe:
    return HostProbe()


def plan_commands(
    report: PrereqReport,
    *,
    image: str,
    runner_image: str,
    ssh_port: int,
    http_port: int,
    no_update: bool,
) -> list[list[str]]:
    """The docker commands `gitt up` will issue, in order. A stopped-but-present container is removed first."""
    hotkey = report.hotkey_ss58 or ''
    if no_update:
        target, cmd = (
            AGENT_CONTAINER_NAME,
            agent_run_command(image=image, ssh_port=ssh_port, http_port=http_port, miner_hotkey=hotkey),
        )
        state = report.agent_state
    else:
        target, cmd = (
            RUNNER_CONTAINER_NAME,
            runner_run_command(
                agent_image=image,
                runner_image=runner_image,
                ssh_port=ssh_port,
                http_port=http_port,
                miner_hotkey=hotkey,
            ),
        )
        state = report.runner_state
    plan = []
    if state is not None and state != 'running':
        plan.append(['docker', 'rm', '-f', target])
    plan.append(cmd)
    return plan


@click.command('up')
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', type=NETWORK_CHOICE, default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--ssh-port', type=int, default=AGENT_SSH_PORT, show_default=True, help='sshd port the controller reaches.'
)
@click.option('--port', 'http_port', type=int, default=AGENT_HTTP_PORT, show_default=True, help='Agent HTTP port.')
@click.option('--image', default=AGENT_IMAGE, show_default=True, help='Agent image the runner follows.')
@click.option('--runner-image', default=RUNNER_IMAGE, show_default=True, help='Runner image.')
@click.option('--no-update', is_flag=True, default=False, help='Start the agent directly, no self-updating runner.')
@click.option('--dry-run', is_flag=True, default=False, help='Print the docker command(s) without running them.')
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def up_command(
    wallet_name,
    wallet_hotkey,
    netuid,
    network,
    rpc_url,
    ssh_port,
    http_port,
    image,
    runner_image,
    no_update,
    dry_run,
    json_mode,
):
    """Start the compute agent: the one container that makes this box a Gittensor compute miner.

    Checks the NVIDIA driver, Docker + the NVIDIA container toolkit, that the SSH and agent ports are free, and
    that your hotkey exists and is registered. Then starts a self-updating runner which pulls the agent image
    and keeps it running. After this you do nothing: the controller installs a per-operation SSH key through the
    agent's signed route and drives the box over SSH.

    \b
    Examples:
        gitt up --wallet alice --hotkey default
        gitt up --dry-run
        gitt up --no-update --image entrius/gt-agent:dev   (a locally built image)
    """
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    endpoint = _resolve_endpoint(network, rpc_url)

    if not json_mode:
        err_console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {endpoint} | Netuid: {netuid}[/dim]')

    report = run_prereqs(
        _make_probe(),
        wallet=wallet_name,
        hotkey=wallet_hotkey,
        netuid=netuid,
        endpoint=endpoint,
        ssh_port=ssh_port,
        http_port=http_port,
        skip_chain=dry_run,
    )
    plan = plan_commands(
        report, image=image, runner_image=runner_image, ssh_port=ssh_port, http_port=http_port, no_update=no_update
    )
    # What the runner itself will issue: printed so the miner can see exactly what runs privileged on their box.
    agent_line = agent_run_command(
        image=image, ssh_port=ssh_port, http_port=http_port, miner_hotkey=report.hotkey_ss58 or ''
    )

    if json_mode:
        emit_json(
            {
                'success': report.ok or dry_run,
                'dry_run': dry_run,
                'already_up': report.already_up,
                'hotkey_ss58': report.hotkey_ss58,
                'checks': [r.as_dict() for r in report.results],
                'commands': [render(c) for c in plan],
                'agent_command': render(agent_line),
            }
        )
    else:
        console.print(render_table(report.results))

    if not report.ok and not dry_run:
        _error('Prerequisites failed; fix the rows marked fail and run `gitt up` again.', json_mode)
        sys.exit(1)

    if dry_run:
        if not json_mode:
            console.print('\n[bold]Would run:[/bold]')
            for cmd in plan:
                click.echo(f'  {render(cmd)}')
            if not no_update:
                console.print('\n[bold]The runner then keeps this agent container running:[/bold]')
                click.echo(f'  {render(agent_line)}')
        return

    if report.already_up:
        if not json_mode:
            err_console.print(
                f'[green]Already up:[/green] {AGENT_CONTAINER_NAME} / {RUNNER_CONTAINER_NAME} are running. `gitt down` stops them.'
            )
        return

    for cmd in plan:
        proc = docker_exec.run_docker(cmd)
        if proc.returncode != 0:
            _error(f'`{render(cmd)}` failed: {(proc.stderr or proc.stdout).strip()[:300]}', json_mode)
            sys.exit(1)

    if not json_mode:
        started = AGENT_CONTAINER_NAME if no_update else RUNNER_CONTAINER_NAME
        err_console.print(f'\n[green]Started {started}.[/green] sshd :{ssh_port}, agent :{http_port}.')
        err_console.print(
            '[dim]Nothing else to do: the controller takes it from here. `gitt down` stops the agent.[/dim]'
        )
