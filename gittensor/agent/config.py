# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Build-time constants for the compute agent (``gitt up`` container) and its runner.

Everything the controller, the agent image, the runner and the CLI must agree on is here: the trust anchors, the
ports, the container and image names, the release channel. Stdlib only — imported by the CLI on the miner's host.
"""

from __future__ import annotations

from gittensor import __version__

AGENT_VERSION = __version__

# --- Trust anchors ---------------------------------------------------------------------------------------------------
# Two keys, both compiled into images, both rotated by shipping an agent release (vault 26 §5-6). Neither is ever an
# environment variable: a host operator must not be able to repoint their agent at another controller at runtime.
#
# 1. The SSH certificate authority. Its public half is baked into the agent image at build (docker/agent/Dockerfile
#    ARG GT_CA_PUB -> /etc/ssh/gt_ca.pub, sshd `TrustedUserCAKeys`). The private half lives only in the controller
#    container and signs a ~5-minute certificate per visit (gittensor/controller/ssh). The agent never sees it.
# 2. The release-signing key. Its public half is what `gitt up` and the runner verify the release channel with
#    (docker/agent/channel: stable.json + stable.json.sig, `ssh-keygen -Y sign`). The private half lives in CI.
#
# RELEASE_PUBKEY_OPENSSH is the real release public key (ceremony 2026-09-14, fingerprint
# SHA256:NFES+v4hRuN2GMTeGBnTFch3jIBP/oEtNzQmi+pqNns). Its private half is the GT_RELEASE_KEY Actions secret. With it
# empty `gitt up` would refuse to start a runner. Dev keys come from docker/agent/keys/make-dev-keys.sh; they carry
# DEV_KEY_MARKER in their comment and the agent refuses to start on one unless GT_AGENT_ALLOW_DEV_KEYS=1.
RELEASE_PUBKEY_OPENSSH = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGqwZgf8OHuUQmyeVLxDXXjLAcjMJwypNEKDEv7ltaLr gittensor-release'
RELEASE_SIGNER_IDENTITY = 'gittensor-release'  # the principal in the runner's allowed_signers file
RELEASE_SIGN_NAMESPACE = 'gt-agent-channel'  # `ssh-keygen -Y sign -n`; a signature for another purpose does not verify
DEV_KEY_MARKER = 'DO-NOT-SHIP'  # in a dev key's comment; images built on one refuse to start without the override
CA_PUBKEY_PATH = '/etc/ssh/gt_ca.pub'  # inside the agent image
ALLOWED_SIGNERS_PATH = '/etc/gt-agent/allowed_signers'  # inside the runner image

# --- Certificates (what the controller mints; the agent only checks them) ---------------------------------------------
CERT_PRINCIPAL = 'root'  # sshd matches the certificate's principal list against the login name
CERT_VALIDITY_S = 300  # ~5 minutes: enough to start a visit; open sessions outlive it (26 §5)
CERT_BACKDATE_S = 60  # valid from now - this, so a slightly slow box clock does not reject a fresh certificate
HUMAN_CERT_VALIDITY_S = 3600  # a person debugging a box gets a longer window; every login is logged by key ID

# --- The container ---------------------------------------------------------------------------------------------------
AGENT_SSH_PORT = 2200  # sshd, root by certificate only (Lium's default port too)
SSH_HOSTKEY_VOLUME = 'gt-agent-ssh'  # named volume: the sshd host key survives self-updates; pinned at ADMIT
SSH_HOSTKEY_VOLUME_MOUNT = '/var/lib/gt-agent'
AGENT_CONTAINER_NAME = 'gt-agent'
RUNNER_CONTAINER_NAME = 'gt-agent-runner'

# --- Images and the release channel --------------------------------------------------------------------------------
# Docker Hub under `entrius` (vault 23 §8, 26 §7). Trust is the digest + the signature, never the registry or a tag:
# the runner runs `entrius/gt-agent@sha256:...` from the signed channel file and never a mutable tag. A fleet
# release is a new stable.json + signature (docker/agent/channel/sign.sh), not a push to a tag.
IMAGE_REGISTRY = 'docker.io'
AGENT_IMAGE_REPO = 'entrius/gt-agent'
RUNNER_IMAGE_REPO = 'entrius/gt-agent-runner'
PROOF_IMAGE_REPO = 'entrius/gt-proof'
AGENT_CHANNEL = 'stable'
AGENT_CHANNEL_URL = (
    f'https://raw.githubusercontent.com/entrius/gittensor/main/docker/agent/channel/{AGENT_CHANNEL}.json'
)
# Tag forms exist for local builds only (`gitt up --no-update --image entrius/gt-agent:dev`).
AGENT_IMAGE = f'{AGENT_IMAGE_REPO}:{AGENT_CHANNEL}'
RUNNER_IMAGE = f'{RUNNER_IMAGE_REPO}:{AGENT_CHANNEL}'
UPDATE_INTERVAL_S = 60  # runner poll interval (Lium: watchtower every minute)
CHANNEL_FETCH_TIMEOUT_S = 15.0

# --- Environment variable names (container side; the CLI and runner.sh set them) --------------------------------------
ENV_SSH_PORT = 'GT_AGENT_SSH_PORT'
ENV_MINER_HOTKEY = 'GT_AGENT_MINER_HOTKEY'
ENV_IMAGE = 'GT_AGENT_IMAGE'
ENV_IMAGE_DIGEST = 'GT_AGENT_IMAGE_DIGEST'
ENV_CONTAINER_NAME = 'GT_AGENT_CONTAINER_NAME'
ENV_UPDATE_INTERVAL = 'GT_AGENT_UPDATE_INTERVAL_S'
ENV_CHANNEL_URL = 'GT_AGENT_CHANNEL_URL'
ENV_ALLOW_DEV_KEYS = 'GT_AGENT_ALLOW_DEV_KEYS'
