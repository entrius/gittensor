# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The ``docker`` lines behind ``gitt up`` / ``gitt down``.

``gitt up`` issues :func:`runner_run_command`; the runner container (``docker/agent/runner.sh``) then issues the
agent line, which :func:`agent_run_command` reproduces here so ``--dry-run`` can print it and tests can hold the
shell script to the same flags. ``gitt up --no-update`` issues :func:`agent_run_command` directly (local builds).

``gitt down`` is a clean leave (Kimbo 9/16; the 9/16 soak left the 27B serving with no agent behind it): it removes the
runner and the agent first, then lists the controller's workload containers on the box (``INSTANCE_LABEL``, named
``gt-i-…``), drains and stops each (SIGTERM, wait up to the manifest's ``drain.max_s`` from the container's
``DRAIN_LABEL``, else ``WORKLOAD_STOP_DEFAULT_S``) and removes them. The agent goes first so the controller can only
ever see "unreachable" and then "gone after unreachable" (a stop, not a cheat), never a container gone under a live
agent. ``gitt up --reclaim`` uses the same drain-and-remove commands on a workload left behind.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from gittensor.agent.config import (
    AGENT_CHANNEL_URL,
    AGENT_CONTAINER_NAME,
    AGENT_IMAGE,
    DRAIN_LABEL,
    DRAIN_MARKER,
    ENV_ALLOW_DEV_KEYS,
    ENV_CHANNEL_URL,
    ENV_CONTAINER_NAME,
    ENV_IMAGE,
    ENV_IMAGE_DIGEST,
    ENV_MINER_HOTKEY,
    ENV_RENT_PORTS,
    ENV_SSH_PORT,
    ENV_UPDATE_INTERVAL,
    ENV_VENDOR,
    INSTANCE_LABEL,
    PORT_LABEL,
    RENT_PORTS_LABEL,
    RENTAL_ENDS_AT_LABEL,
    RUNNER_CONTAINER_NAME,
    RUNNER_IMAGE,
    SSH_HOSTKEY_VOLUME,
    SSH_HOSTKEY_VOLUME_MOUNT,
    UPDATE_INTERVAL_S,
    WORKLOAD_STOP_DEFAULT_S,
    rent_ports_label,
)
from gittensor.controller.checks.vendor import AMD, NVIDIA

DOCKER_SOCK = '/var/run/docker.sock'

# The privileges the agent needs and nothing more is not a claim we can make: this is Lium's executor footprint
# (vault 22 §4). Privileged + pid host + docker.sock is host root for whoever holds the controller's CA key.
AGENT_PRIVILEGE_FLAGS = ('--privileged', '--pid', 'host')
# The NVIDIA container runtime attaches every card to the agent (nvidia-smi, the scrape). An AMD box has no such
# runtime and ``docker run --gpus all`` fails outright there; ``--privileged`` already exposes ``/dev/kfd`` and
# ``/dev/dri`` (vault 30 §1 #5), so on an AMD box the flag is simply left off.
NVIDIA_GPU_FLAGS = ('--gpus', 'all')


def agent_run_command(
    *,
    image: str = AGENT_IMAGE,
    ssh_port: int,
    miner_hotkey: str = '',
    image_digest: str = '',
    name: str = AGENT_CONTAINER_NAME,
    allow_dev_keys: bool = False,
    rent_ports: tuple[int, int] | None = None,
    vendor: str = NVIDIA,
) -> list[str]:
    """The agent container: what the runner starts (and restarts when the channel's digest moves). One published port,
    sshd. ``allow_dev_keys`` lets an image built on docker/agent/keys/make-dev-keys.sh keys start (local builds).
    ``rent_ports`` (``gitt up --rent``) is carried as a label for the controller to read: the box offers itself for
    rental on that range (vault 29). ``vendor`` is the host's (``gitt up`` detects it as the controller's scrape does);
    ``amd`` leaves ``--gpus all`` off and tells the agent its vendor instead of the NVIDIA capabilities env.
    docker/agent/runner.sh issues the same line; keep them together."""
    cmd = [
        'docker',
        'run',
        '-d',
        '--name',
        name,
        '--restart',
        'unless-stopped',
        *AGENT_PRIVILEGE_FLAGS,
        *(() if vendor == AMD else NVIDIA_GPU_FLAGS),
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
        f'{ENV_VENDOR}={AMD}' if vendor == AMD else 'NVIDIA_DRIVER_CAPABILITIES=all',
    ]
    if allow_dev_keys:
        cmd += ['-e', f'{ENV_ALLOW_DEV_KEYS}=1']
    if rent_ports:
        cmd += ['--label', f'{RENT_PORTS_LABEL}={rent_ports_label(rent_ports)}']
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
    rent_ports: tuple[int, int] | None = None,
    vendor: str = NVIDIA,
) -> list[str]:
    """The runner container: what ``gitt up`` actually issues. Needs only the docker socket. It follows the signed
    channel at ``channel_url``, never a tag. ``rent_ports`` and an AMD ``vendor`` are handed on to every agent it
    starts."""
    rent = ['-e', f'{ENV_RENT_PORTS}={rent_ports_label(rent_ports)}'] if rent_ports else []
    amd = ['-e', f'{ENV_VENDOR}={AMD}'] if vendor == AMD else []
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
        *rent,
        *amd,
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
    ends_at: float | None = None  # a customer's pod: RENTAL_ENDS_AT_LABEL, when the rental is due to end

    @property
    def running(self) -> bool:
        return self.state in ('running', 'paused')


_PS_FORMAT = '\t'.join(
    (
        '{{.ID}}',
        '{{.Names}}',
        '{{.State}}',
        f'{{{{.Label "{PORT_LABEL}"}}}}',
        f'{{{{.Label "{DRAIN_LABEL}"}}}}',
        f'{{{{.Label "{RENTAL_ENDS_AT_LABEL}"}}}}',
    )
)


def workload_list_command(label: str = INSTANCE_LABEL) -> list[str]:
    """Our containers on the box by label: placement instances (``INSTANCE_LABEL``) or customers' pods
    (``RENTAL_LABEL``, vault 29; no port or drain label, so a pod drains with ``WORKLOAD_STOP_DEFAULT_S``)."""
    return ['docker', 'ps', '-a', '--no-trunc', '--filter', f'label={label}', '--format', _PS_FORMAT]


def parse_workloads(stdout: str) -> list[Workload]:
    out = []
    for line in stdout.splitlines():
        cols = line.rstrip('\n').split('\t')
        if len(cols) not in (5, 6) or not cols[0]:  # 5: a listing from before the ends_at label
            continue
        port = int(cols[3]) if cols[3].isdigit() else None
        drain = int(cols[4]) if cols[4].isdigit() else None
        ends_at = _number(cols[5]) if len(cols) == 6 else None
        out.append(Workload(cols[0], cols[1], cols[2], port, drain, ends_at))
    return out


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def drain_mark_command(agent_name: str = AGENT_CONTAINER_NAME) -> list[str]:
    """Tell the controller this box takes no new rental: the marker in the agent's volume (``DRAIN_MARKER``), read with
    the rent-ports label on its next visit."""
    return ['docker', 'exec', agent_name, 'touch', DRAIN_MARKER]


def drain_clear_command(image: str) -> list[str]:
    """`gitt up --rent`: the box is for rent again. Through a throwaway container on the volume, because the agent may
    not be running yet (the runner starts it); ``image`` is the one `gitt up` is about to run, so it is present."""
    return [
        'docker',
        'run',
        '--rm',
        '-v',
        f'{SSH_HOSTKEY_VOLUME}:{SSH_HOSTKEY_VOLUME_MOUNT}',
        '--entrypoint',
        'rm',
        image,
        '-f',
        DRAIN_MARKER,
    ]


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
    """The runner first (so it cannot resurrect the agent), then the agent, then our workloads and any customer's pod
    (drained, then removed, so no orphan holds a port for the next `gitt up`). The agent goes before the workloads on purpose: with
    the agent gone the controller can only ever see "unreachable" and then "gone after unreachable" (the lease ends at
    the last good heartbeat, no bench); a workload removed under a live agent looks like a killed placement."""
    return [
        ['docker', 'rm', '-f', runner_name],
        ['docker', 'rm', '-f', agent_name],
        *workload_stop_commands(workloads or [], now),
    ]


def render(command: list[str]) -> str:
    return shlex.join(command)
