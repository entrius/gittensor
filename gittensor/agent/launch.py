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
    AGENT_CHANNEL_URL,
    AGENT_CONTAINER_NAME,
    AGENT_IMAGE,
    ENV_ALLOW_DEV_KEYS,
    ENV_CHANNEL_URL,
    ENV_CONTAINER_NAME,
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


def down_commands(agent_name: str = AGENT_CONTAINER_NAME, runner_name: str = RUNNER_CONTAINER_NAME) -> list[list[str]]:
    """Stop the runner first so it cannot resurrect the agent while we remove it."""
    return [['docker', 'rm', '-f', runner_name], ['docker', 'rm', '-f', agent_name]]


def render(command: list[str]) -> str:
    return shlex.join(command)
