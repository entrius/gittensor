# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The ``docker`` lines behind ``gitt up`` / ``gitt down``.

``gitt up`` issues :func:`runner_run_command`; the runner container (``docker/agent/runner.sh``) then issues the
agent line, which :func:`agent_run_command` reproduces here so ``--dry-run`` can print it and tests can hold the
shell script to the same flags. ``gitt up --no-update`` issues :func:`agent_run_command` directly (local builds).

``gitt down`` is a clean leave (Kimbo 9/16; the 9/16 soak left the 27B serving with no agent behind it): it lists the
controller's workload containers on the box (``INSTANCE_LABEL``, named ``gt-i-…``), drains and stops each (SIGTERM,
wait up to the manifest's ``drain.max_s`` from the container's ``DRAIN_LABEL``, else ``WORKLOAD_STOP_DEFAULT_S``),
removes them, then the runner and the agent. ``gitt up --reclaim`` uses the same commands on a workload left behind.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from gittensor.agent.config import (
    AGENT_CHANNEL_URL,
    AGENT_CONTAINER_NAME,
    AGENT_IMAGE,
    DRAIN_LABEL,
    ENV_ALLOW_DEV_KEYS,
    ENV_CHANNEL_URL,
    ENV_CONTAINER_NAME,
    ENV_IMAGE,
    ENV_IMAGE_DIGEST,
    ENV_MINER_HOTKEY,
    ENV_SSH_PORT,
    ENV_UPDATE_INTERVAL,
    INSTANCE_LABEL,
    PORT_LABEL,
    RUNNER_CONTAINER_NAME,
    RUNNER_IMAGE,
    SSH_HOSTKEY_VOLUME,
    SSH_HOSTKEY_VOLUME_MOUNT,
    UPDATE_INTERVAL_S,
    WORKLOAD_STOP_DEFAULT_S,
)

DOCKER_SOCK = '/var/run/docker.sock'

# The privileges the agent needs and nothing more is not a claim we can make: this is Lium's executor footprint
# (vault 22 §4). Privileged + pid host + docker.sock is host root for whoever holds the controller's CA key.
AGENT_PRIVILEGE_FLAGS = ('--privileged', '--pid', 'host', '--gpus', 'all')


def agent_run_command(
    *,
    image: str = AGENT_IMAGE,
    ssh_port: int,
    miner_hotkey: str = '',
    image_digest: str = '',
    name: str = AGENT_CONTAINER_NAME,
    allow_dev_keys: bool = False,
) -> list[str]:
    """The agent container: what the runner starts (and restarts when the channel's digest moves). One published port,
    sshd. ``allow_dev_keys`` lets an image built on docker/agent/keys/make-dev-keys.sh keys start (local builds)."""
    cmd = [
        'docker',
        'run',
        '-d',
        '--name',
        name,
        '--restart',
        'unless-stopped',
        *AGENT_PRIVILEGE_FLAGS,
        '-v',
        f'{DOCKER_SOCK}:{DOCKER_SOCK}',
        '-v',
        f'{SSH_HOSTKEY_VOLUME}:{SSH_HOSTKEY_VOLUME_MOUNT}',
        '-p',
        f'{ssh_port}:{ssh_port}',
        '-e',
        f'{ENV_SSH_PORT}={ssh_port}',
        '-e',
        f'{ENV_MINER_HOTKEY}={miner_hotkey}',
        '-e',
        f'{ENV_IMAGE}={image}',
        '-e',
        f'{ENV_IMAGE_DIGEST}={image_digest}',
        '-e',
        'NVIDIA_DRIVER_CAPABILITIES=all',
    ]
    if allow_dev_keys:
        cmd += ['-e', f'{ENV_ALLOW_DEV_KEYS}=1']
    return [*cmd, image]


def runner_run_command(
    *,
    runner_image: str = RUNNER_IMAGE,
    ssh_port: int,
    miner_hotkey: str = '',
    channel_url: str = AGENT_CHANNEL_URL,
    update_interval_s: int = UPDATE_INTERVAL_S,
    agent_name: str = AGENT_CONTAINER_NAME,
    name: str = RUNNER_CONTAINER_NAME,
) -> list[str]:
    """The runner container: what ``gitt up`` actually issues. Needs only the docker socket. It follows the signed
    channel at ``channel_url``, never a tag."""
    return [
        'docker',
        'run',
        '-d',
        '--name',
        name,
        '--restart',
        'unless-stopped',
        '-v',
        f'{DOCKER_SOCK}:{DOCKER_SOCK}',
        '-e',
        f'{ENV_CHANNEL_URL}={channel_url}',
        '-e',
        f'{ENV_CONTAINER_NAME}={agent_name}',
        '-e',
        f'{ENV_SSH_PORT}={ssh_port}',
        '-e',
        f'{ENV_MINER_HOTKEY}={miner_hotkey}',
        '-e',
        f'{ENV_UPDATE_INTERVAL}={update_interval_s}',
        runner_image,
    ]


@dataclass(frozen=True)
class Workload:
    """One of the controller's workload containers on this box, as ``docker ps`` lists it."""

    container_id: str
    name: str
    state: str  # running, exited, ...
    port: int | None  # the host port it is published on (PORT_LABEL)
    drain_max_s: int | None  # DRAIN_LABEL; None: started before the label existed

    @property
    def running(self) -> bool:
        return self.state in ('running', 'paused')


_PS_FORMAT = '\t'.join(
    ('{{.ID}}', '{{.Names}}', '{{.State}}', f'{{{{.Label "{PORT_LABEL}"}}}}', f'{{{{.Label "{DRAIN_LABEL}"}}}}')
)


def workload_list_command() -> list[str]:
    return ['docker', 'ps', '-a', '--no-trunc', '--filter', f'label={INSTANCE_LABEL}', '--format', _PS_FORMAT]


def parse_workloads(stdout: str) -> list[Workload]:
    out = []
    for line in stdout.splitlines():
        cols = line.rstrip('\n').split('\t')
        if len(cols) != 5 or not cols[0]:
            continue
        port = int(cols[3]) if cols[3].isdigit() else None
        drain = int(cols[4]) if cols[4].isdigit() else None
        out.append(Workload(cols[0], cols[1], cols[2], port, drain))
    return out


def workload_stop_commands(workloads: list[Workload], now: bool = False) -> list[list[str]]:
    """Drain and remove ``workloads``: one ``docker stop --time <drain.max_s>`` per running container (SIGTERM, then
    the wait its label names; a ``kill`` drain, 0, skips the wait), then ``docker rm -f`` each. ``now`` skips every
    wait."""
    plan: list[list[str]] = []
    if not now:
        for w in workloads:
            wait = w.drain_max_s if w.drain_max_s is not None else WORKLOAD_STOP_DEFAULT_S
            if w.running and wait > 0:
                plan.append(['docker', 'stop', '--time', str(wait), w.container_id])
    plan += [['docker', 'rm', '-f', w.container_id] for w in workloads]
    return plan


def down_commands(
    agent_name: str = AGENT_CONTAINER_NAME,
    runner_name: str = RUNNER_CONTAINER_NAME,
    workloads: list[Workload] | None = None,
    now: bool = False,
) -> list[list[str]]:
    """Our workloads first (drained, then removed, so nothing keeps serving with no agent behind it and no orphan holds
    a workload port for the next `gitt up`), then the runner (so it cannot resurrect the agent), then the agent."""
    return [
        *workload_stop_commands(workloads or [], now),
        ['docker', 'rm', '-f', runner_name],
        ['docker', 'rm', '-f', agent_name],
    ]


def render(command: list[str]) -> str:
    return shlex.join(command)
