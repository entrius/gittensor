# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Build-time constants and runtime settings for the compute agent (``gitt up`` container).

Everything the controller and the agent must agree on is here: the trust anchor, the signed-message format, the
ports, the image names the runner follows. Stdlib only — this module is imported by the CLI on the miner's host
and by the agent inside its image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from gittensor import __version__

AGENT_VERSION = __version__

# --- Trust anchor ------------------------------------------------------------------------------------------------
# The ONE hotkey whose signatures install SSH keys (vault 23 §8: one controller, pinned in the agent). Deliberately
# a constant and not an environment variable — a host operator must not be able to repoint their agent at another
# controller at runtime (Lium does the same, executor/src/core/config.py). Rotating it means shipping an agent
# release.
#
# PLACEHOLDER — derived from the public dev URI `//GittensorControllerDev`, so ANYONE can sign as it. Fine for the
# unpublished dev images this branch builds against our own cards; it MUST be replaced with the real controller
# hotkey's PUBLIC ss58 before any image runs on a box reachable from the internet.
CONTROLLER_HOTKEY_SS58 = '5DUXjxv65aGihpTFqA2KttXAAA4usdctuDXMV7FyTYMr4wAo'

# --- Signed requests ---------------------------------------------------------------------------------------------
SIGNING_DOMAIN = 'gittensor-agent/v1'  # first line of every signed message; a new format bumps it
ACTION_INSTALL = 'install_ssh_key'
ACTION_REMOVE = 'remove_ssh_key'
SIGNATURE_MAX_SKEW_S = 60  # |agent clock - request timestamp| beyond this is a replay or a broken clock
NONCE_MIN_LEN = 16
NONCE_MAX_LEN = 128

# --- The container -----------------------------------------------------------------------------------------------
AGENT_HTTP_PORT = 8200  # the signed route + /info (attest sidecar is 8081, axon 8091, sparkinfer 8000)
AGENT_SSH_PORT = 2200  # sshd, root by key only (Lium's default too)
AUTHORIZED_KEYS_PATH = '/root/.ssh/authorized_keys'
KEY_COMMENT_TAG = 'gittensor-controller'  # comment on every key the agent installs
SSH_HOSTKEY_VOLUME = 'gt-agent-ssh'  # named volume: the sshd host key survives self-updates
SSH_HOSTKEY_VOLUME_MOUNT = '/var/lib/gt-agent'
AGENT_CONTAINER_NAME = 'gt-agent'
RUNNER_CONTAINER_NAME = 'gt-agent-runner'

# --- Images (unpublished; names reserved) --------------------------------------------------------------------------
# The runner follows AGENT_IMAGE_TAG on the registry and recreates the agent whenever the tag's digest moves, so
# the tag is a release channel, not an immutable pin. A fleet flag day is a push to this tag.
IMAGE_REGISTRY = 'docker.io'
AGENT_IMAGE_REPO = 'entrius/gt-agent'
RUNNER_IMAGE_REPO = 'entrius/gt-agent-runner'
AGENT_IMAGE_TAG = 'stable'
AGENT_IMAGE = f'{AGENT_IMAGE_REPO}:{AGENT_IMAGE_TAG}'
RUNNER_IMAGE = f'{RUNNER_IMAGE_REPO}:{AGENT_IMAGE_TAG}'
UPDATE_INTERVAL_S = 60  # runner poll interval (Lium: watchtower every minute)

# --- Environment variable names (container side; the CLI and runner.sh set them) ------------------------------------
ENV_HTTP_PORT = 'GT_AGENT_HTTP_PORT'
ENV_SSH_PORT = 'GT_AGENT_SSH_PORT'
ENV_MINER_HOTKEY = 'GT_AGENT_MINER_HOTKEY'
ENV_IMAGE = 'GT_AGENT_IMAGE'
ENV_IMAGE_DIGEST = 'GT_AGENT_IMAGE_DIGEST'
ENV_CONTAINER_NAME = 'GT_AGENT_CONTAINER_NAME'
ENV_UPDATE_INTERVAL = 'GT_AGENT_UPDATE_INTERVAL_S'
ENV_AUTHORIZED_KEYS = 'GT_AGENT_AUTHORIZED_KEYS'  # dev/test override of AUTHORIZED_KEYS_PATH
ENV_BIND_HOST = 'GT_AGENT_BIND_HOST'


@dataclass(frozen=True)
class AgentSettings:
    """What one running agent knows about itself; :meth:`from_env` is how ``neurons/agent.py`` builds it."""

    http_port: int = AGENT_HTTP_PORT
    ssh_port: int = AGENT_SSH_PORT
    miner_hotkey: str = ''
    image: str = AGENT_IMAGE
    image_digest: str = ''
    authorized_keys_path: str = AUTHORIZED_KEYS_PATH
    bind_host: str = '0.0.0.0'

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AgentSettings:
        env = os.environ if env is None else env
        return cls(
            http_port=int(env.get(ENV_HTTP_PORT, AGENT_HTTP_PORT)),
            ssh_port=int(env.get(ENV_SSH_PORT, AGENT_SSH_PORT)),
            miner_hotkey=env.get(ENV_MINER_HOTKEY, ''),
            image=env.get(ENV_IMAGE, AGENT_IMAGE),
            image_digest=env.get(ENV_IMAGE_DIGEST, ''),
            authorized_keys_path=env.get(ENV_AUTHORIZED_KEYS, AUTHORIZED_KEYS_PATH),
            bind_host=env.get(ENV_BIND_HOST, '0.0.0.0'),
        )
