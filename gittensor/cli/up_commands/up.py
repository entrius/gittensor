# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt up — check prerequisites, publish the box on chain, then start the compute agent. The miner does nothing after
this."""

from __future__ import annotations

import sys

import click

from gittensor.agent import channel as release_channel
from gittensor.agent.config import (
    AGENT_CHANNEL_URL,
    AGENT_CONTAINER_NAME,
    AGENT_IMAGE,
    AGENT_SSH_PORT,
    RUNNER_CONTAINER_NAME,
    WORKLOAD_PORT_RANGE,
)
from gittensor.agent.launch import agent_run_command, render, runner_run_command
from gittensor.cli.helpers import NETWORK_CHOICE, console, err_console
from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.helpers import NETUID_DEFAULT, _error, _load_config_value, _resolve_endpoint

from . import docker_exec
from .prereqs import CheckResult, HostProbe, PrereqReport, render_table, run_prereqs, run_publish_prereqs

ENDPOINT_CHECK = 'Endpoint published'


def _make_probe() -> HostProbe:
    return HostProbe()


def _load_channel(url: str) -> release_channel.Channel:
    return release_channel.load(url)


def plan_commands(
    report: PrereqReport,
    *,
    image: str,
    ssh_port: int,
    no_update: bool,
    allow_dev_keys: bool = False,
    channel: release_channel.Channel | None = None,
    channel_url: str = AGENT_CHANNEL_URL,
) -> list[list[str]]:
    """The docker commands `gitt up` will issue, in order. A stopped-but-present container is removed first.

    With the runner (the default) the runner image comes from the verified ``channel``, by digest; ``image`` is
    only used by ``--no-update`` (a local build started directly)."""
    hotkey = report.hotkey_ss58 or ''
    if no_update:
        target = AGENT_CONTAINER_NAME
        cmd = agent_run_command(image=image, ssh_port=ssh_port, miner_hotkey=hotkey, allow_dev_keys=allow_dev_keys)
        state = report.agent_state
    else:
        if channel is None:
            raise ValueError('a verified channel is required to start the runner')
        target = RUNNER_CONTAINER_NAME
        cmd = runner_run_command(
            runner_image=channel.runner, ssh_port=ssh_port, miner_hotkey=hotkey, channel_url=channel_url
        )
        state = report.runner_state
    plan = []
    if state is not None and state != 'running':
        plan.append(['docker', 'rm', '-f', target])
    plan.append(cmd)
    return plan


def publish_endpoint(
    probe: HostProbe, *, wallet: str, hotkey: str, ss58: str, netuid: int, endpoint: str, ip: str, port: int
) -> tuple[CheckResult, str]:
    """Serve ``ip:port`` as the hotkey's compute endpoint unless the chain already holds exactly that. Returns the table
    row and ``served`` / ``unchanged`` / ``error``."""
    try:
        current = probe.chain_endpoint(ss58, netuid, endpoint)
    except Exception as e:  # network / RPC trouble: report, do not crash the table
        return CheckResult(ENDPOINT_CHECK, False, f'axon lookup failed against {endpoint}: {e}'[:160]), 'error'
    if current == (ip, port, True):
        return CheckResult(ENDPOINT_CHECK, True, f'{ip}:{port} on netuid {netuid}, unchanged on chain'), 'unchanged'
    try:
        error = probe.serve(wallet, hotkey, netuid, endpoint, ip, port)
    except Exception as e:
        error = f'{type(e).__name__}: {e}'
    if error:
        return CheckResult(ENDPOINT_CHECK, False, f'serve_axon {ip}:{port} failed: {error}'[:160]), 'error'
    was = f' (was {current[0]}:{current[1]})' if current else ''
    return CheckResult(ENDPOINT_CHECK, True, f'served {ip}:{port} on netuid {netuid}{was}'), 'served'


@click.command('up')
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', type=NETWORK_CHOICE, default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--ssh-port', type=int, default=AGENT_SSH_PORT, show_default=True, help='sshd port the controller reaches.'
)
@click.option('--ip', 'public_ip', default=None, help="Public IP to publish on chain (default: this box's, detected).")
@click.option(
    '--skip-reachability',
    is_flag=True,
    default=False,
    help='Skip the best-effort check that the sshd port answers on the public IP.',
)
@click.option('--channel-url', default=AGENT_CHANNEL_URL, show_default=True, help='Signed release channel to follow.')
@click.option('--image', default=AGENT_IMAGE, show_default=True, help='Agent image for --no-update (a local build).')
@click.option('--no-update', is_flag=True, default=False, help='Start the agent directly, no self-updating runner.')
@click.option(
    '--allow-dev-keys',
    is_flag=True,
    default=False,
    help='Let an agent image built on docker/agent/keys/make-dev-keys.sh keys start (with --no-update only).',
)
@click.option(
    '--no-chain',
    is_flag=True,
    default=False,
    help='Dev boxes only (with --no-update): no registration lookup, nothing published on chain, no hotkey needed.',
)
@click.option(
    '--publish-only',
    is_flag=True,
    default=False,
    help='Only the chain step, from a machine holding the wallet: publish --ip and --ssh-port for a box elsewhere.',
)
@click.option(
    '--dry-run', is_flag=True, default=False, help='Print what would be published and run, without doing either.'
)
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def up_command(
    wallet_name,
    wallet_hotkey,
    netuid,
    network,
    rpc_url,
    ssh_port,
    public_ip,
    skip_reachability,
    channel_url,
    image,
    no_update,
    allow_dev_keys,
    no_chain,
    publish_only,
    dry_run,
    json_mode,
):
    """Start the compute agent: the one container that makes this box a Gittensor compute miner.

    Checks the NVIDIA driver, Docker + the NVIDIA container toolkit, that the SSH port and the workload port range
    are free, your public IP, and that your hotkey exists and is registered. Then publishes this box on chain (your
    public IP and the sshd port as your hotkey's axon, signed by your hotkey; re-run only when they change), verifies
    the signed release channel and starts a self-updating runner which pulls the agent image by digest and keeps it
    running. After this you do nothing: the controller finds the box on chain, logs in over SSH with a short-lived
    certificate and drives it.

    \b
    Open on your firewall / router, TCP from the internet, and nothing else:
        the sshd port (--ssh-port, default {ssh})
        the workload ports {low}-{high} (the controller publishes each instance on one of them)
    A home connection behind carrier-grade NAT cannot be reached and cannot join as-is.

    \b
    Examples:
        gitt up --wallet alice --hotkey default
        gitt up --dry-run
        gitt up --no-update --allow-dev-keys --no-chain --image entrius/gt-agent:dev   (a locally built dev box)
    """
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    endpoint = _resolve_endpoint(network, rpc_url)
    if allow_dev_keys and not no_update:
        _error('--allow-dev-keys only applies to --no-update (a locally built image).', json_mode)
        sys.exit(2)
    if no_chain and not no_update:
        _error('--no-chain only applies to --no-update (a dev box running a local build).', json_mode)
        sys.exit(2)
    if publish_only:
        if no_update or no_chain:
            _error('--publish-only only publishes: drop --no-update / --no-chain / --allow-dev-keys.', json_mode)
            sys.exit(2)
        if not public_ip:
            _error("--publish-only needs --ip: the box's public address (this machine is not the box).", json_mode)
            sys.exit(2)
        _publish_only(
            wallet_name, wallet_hotkey, netuid, endpoint, public_ip, ssh_port, skip_reachability, dry_run, json_mode
        )
        return

    if not json_mode:
        err_console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {endpoint} | Netuid: {netuid}[/dim]')
        if no_chain:
            err_console.print(
                '[bold red]WARNING: --no-chain — the hotkey registration lookup is SKIPPED and nothing is published. '
                'This is a dev box, not a miner: nothing it does earns or is scored.[/bold red]'
            )

    probe = _make_probe()
    report = run_prereqs(
        probe,
        wallet=wallet_name,
        hotkey=wallet_hotkey,
        netuid=netuid,
        endpoint=endpoint,
        ssh_port=ssh_port,
        skip_chain=dry_run,
        no_chain=no_chain,
        public_ip=public_ip,
        skip_reachability=skip_reachability,
    )

    channel = None
    channel_error = None
    if not no_update:
        try:
            channel = _load_channel(channel_url)
        except release_channel.ChannelError as e:
            channel_error = str(e)
        report.results.append(
            release_channel_result(channel, channel_error, channel_url)  # a row in the table like any other check
        )

    # Publish after every other check passed, before anything starts: a box the controller cannot find is no miner.
    published = 'skipped'
    if no_chain:
        report.results.append(CheckResult(ENDPOINT_CHECK, None, 'skipped (--no-chain: nothing published)'))
    elif dry_run:
        detail = f'would publish {report.public_ip}:{ssh_port} on netuid {netuid}' if report.public_ip else 'no IP'
        report.results.append(CheckResult(ENDPOINT_CHECK, None, detail))
    elif report.ok:
        row, published = publish_endpoint(
            probe,
            wallet=wallet_name,
            hotkey=wallet_hotkey,
            ss58=report.hotkey_ss58 or '',
            netuid=netuid,
            endpoint=endpoint,
            ip=report.public_ip or '',
            port=ssh_port,
        )
        report.results.append(row)

    if channel is None and not no_update:
        plan: list[list[str]] = []
        agent_line: list[str] = []
    else:
        plan = plan_commands(
            report,
            image=image,
            ssh_port=ssh_port,
            no_update=no_update,
            allow_dev_keys=allow_dev_keys,
            channel=channel,
            channel_url=channel_url,
        )
        # What the runner itself will issue: printed so the miner can see exactly what runs privileged on their box.
        agent_image, agent_digest = (
            (image, '') if no_update or channel is None else (channel.agent, channel.agent_digest)
        )
        agent_line = agent_run_command(
            image=agent_image,
            ssh_port=ssh_port,
            miner_hotkey=report.hotkey_ss58 or '',
            image_digest=agent_digest,
            allow_dev_keys=allow_dev_keys,
        )

    if json_mode:
        emit_json(
            {
                'success': report.ok or dry_run,
                'dry_run': dry_run,
                'no_chain': no_chain,
                'already_up': report.already_up,
                'hotkey_ss58': report.hotkey_ss58,
                'endpoint': {
                    'ip': report.public_ip,
                    'port': ssh_port,
                    'netuid': netuid,
                    'workload_ports': list(WORKLOAD_PORT_RANGE),
                    'published': published,
                },
                'channel': None if channel is None else channel.__dict__,
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
            if plan:
                console.print('\n[bold]Would run:[/bold]')
                for cmd in plan:
                    click.echo(f'  {render(cmd)}')
                if not no_update:
                    console.print('\n[bold]The runner then keeps this agent container running:[/bold]')
                    click.echo(f'  {render(agent_line)}')
            else:
                console.print('\n[yellow]No channel: nothing to run.[/yellow]')
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
        low, high = WORKLOAD_PORT_RANGE
        err_console.print(f'\n[green]Started {started}.[/green] sshd :{ssh_port}, workload ports {low}-{high}.')
        err_console.print(
            '[dim]Nothing else to do: the controller takes it from here. `gitt down` stops the agent.[/dim]'
        )


up_command.help = (up_command.help or '').format(
    ssh=AGENT_SSH_PORT, low=WORKLOAD_PORT_RANGE[0], high=WORKLOAD_PORT_RANGE[1]
)


def _publish_only(wallet, hotkey, netuid, endpoint, ip, ssh_port, skip_reachability, dry_run, json_mode):
    """`gitt up --publish-only`: the chain step alone, from the machine holding the wallet, for a box elsewhere. Same
    marker and same re-serve-only-on-change rule as a full `gitt up`; nothing is checked or started on this machine."""
    probe = _make_probe()
    report = run_publish_prereqs(
        probe,
        wallet=wallet,
        hotkey=hotkey,
        netuid=netuid,
        endpoint=endpoint,
        public_ip=ip,
        ssh_port=ssh_port,
        skip_chain=dry_run,
        skip_reachability=skip_reachability,
    )
    published = 'skipped'
    if dry_run:
        detail = f'would publish {report.public_ip}:{ssh_port} on netuid {netuid}' if report.public_ip else 'no IP'
        report.results.append(CheckResult(ENDPOINT_CHECK, None, detail))
    elif report.ok:
        row, published = publish_endpoint(
            probe,
            wallet=wallet,
            hotkey=hotkey,
            ss58=report.hotkey_ss58 or '',
            netuid=netuid,
            endpoint=endpoint,
            ip=report.public_ip or '',
            port=ssh_port,
        )
        report.results.append(row)

    if json_mode:
        emit_json(
            {
                'success': report.ok or dry_run,
                'dry_run': dry_run,
                'publish_only': True,
                'hotkey_ss58': report.hotkey_ss58,
                'endpoint': {
                    'ip': report.public_ip,
                    'port': ssh_port,
                    'netuid': netuid,
                    'workload_ports': list(WORKLOAD_PORT_RANGE),
                    'published': published,
                },
                'checks': [r.as_dict() for r in report.results],
                'commands': [],
                'agent_command': '',
            }
        )
    else:
        console.print(render_table(report.results))
    if not report.ok and not dry_run:
        _error('Publishing failed; fix the rows marked fail and run `gitt up --publish-only` again.', json_mode)
        sys.exit(1)


def release_channel_result(channel, error, url):
    if channel is not None:
        return CheckResult('Release channel', True, f'{channel.version or "?"} → {channel.agent[-19:]} ({url})')
    return CheckResult('Release channel', False, error[:160])
