# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The ``docker run`` lines behind ``gitt up`` / ``gitt down``.

``gitt up`` issues :func:`runner_run_command`; the runner container (``docker/agent/runner.sh``) then issues the
agent line, which :func:`agent_run_command` reproduces here so ``--dry-run`` can print it and tests can hold the
shell script to the same flags. ``gitt up --no-update`` issues :func:`agent_run_command` directly (local builds).
"""

from __future__ import annotations

import shlex

from gittensor.agent.config import (
    AGENT_CONTAINER_NAME,
    AGENT_IMAGE,
    ENV_CONTAINER_NAME,
    ENV_HTTP_PORT,
    ENV_IMAGE,
    ENV_IMAGE_DIGEST,
    ENV_MINER_HOTKEY,
    ENV_SSH_PORT,
    ENV_UPDATE_INTERVAL,
    RUNNER_CONTAINER_NAME,
    RUNNER_IMAGE,
    SSH_HOSTKEY_VOLUME,
    SSH_HOSTKEY_VOLUME_MOUNT,
    UPDATE_INTERVAL_S,
)

DOCKER_SOCK = '/var/run/docker.sock'

# The privileges the agent needs and nothing more is not a claim we can make: this is Lium's executor footprint
# (vault 22 §4). Privileged + pid host + docker.sock is host root for whoever holds the controller hotkey.
AGENT_PRIVILEGE_FLAGS = ('--privileged', '--pid', 'host', '--gpus', 'all')


def agent_run_command(
    *,
    image: str = AGENT_IMAGE,
    ssh_port: int,
    http_port: int,
    miner_hotkey: str = '',
    image_digest: str = '',
    name: str = AGENT_CONTAINER_NAME,
) -> list[str]:
    """The agent container: what the runner starts (and restarts when the image digest moves)."""
    return [
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
        '-p',
        f'{http_port}:{http_port}',
        '-e',
        f'{ENV_SSH_PORT}={ssh_port}',
        '-e',
        f'{ENV_HTTP_PORT}={http_port}',
        '-e',
        f'{ENV_MINER_HOTKEY}={miner_hotkey}',
        '-e',
        f'{ENV_IMAGE}={image}',
        '-e',
        f'{ENV_IMAGE_DIGEST}={image_digest}',
        '-e',
        'NVIDIA_DRIVER_CAPABILITIES=all',
        image,
    ]


def runner_run_command(
    *,
    agent_image: str = AGENT_IMAGE,
    runner_image: str = RUNNER_IMAGE,
    ssh_port: int,
    http_port: int,
    miner_hotkey: str = '',
    update_interval_s: int = UPDATE_INTERVAL_S,
    agent_name: str = AGENT_CONTAINER_NAME,
    name: str = RUNNER_CONTAINER_NAME,
) -> list[str]:
    """The runner container: what ``gitt up`` actually issues. Needs only the docker socket."""
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
        f'{ENV_IMAGE}={agent_image}',
        '-e',
        f'{ENV_CONTAINER_NAME}={agent_name}',
        '-e',
        f'{ENV_SSH_PORT}={ssh_port}',
        '-e',
        f'{ENV_HTTP_PORT}={http_port}',
        '-e',
        f'{ENV_MINER_HOTKEY}={miner_hotkey}',
        '-e',
        f'{ENV_UPDATE_INTERVAL}={update_interval_s}',
        runner_image,
    ]


def down_commands(agent_name: str = AGENT_CONTAINER_NAME, runner_name: str = RUNNER_CONTAINER_NAME) -> list[list[str]]:
    """Stop the runner first so it cannot resurrect the agent while we remove it."""
    return [['docker', 'rm', '-f', runner_name], ['docker', 'rm', '-f', agent_name]]


def render(command: list[str]) -> str:
    return shlex.join(command)
