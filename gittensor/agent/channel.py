# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The signed release channel the runner (and ``gitt up``) follow instead of a mutable image tag.

``stable.json`` names the agent and runner images by digest; ``stable.json.sig`` is an OpenSSH signature over the
exact bytes of the file (``ssh-keygen -Y sign -n gt-agent-channel``). A verifier needs only ``ssh-keygen`` and the
release public key; both the runner image (``docker/agent/runner.sh``) and this module do the same check. A channel
that does not verify is ignored and whatever is running stays running.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gittensor.agent.config import (
    AGENT_IMAGE_REPO,
    CHANNEL_FETCH_TIMEOUT_S,
    RELEASE_PUBKEY_OPENSSH,
    RELEASE_SIGN_NAMESPACE,
    RELEASE_SIGNER_IDENTITY,
    RUNNER_IMAGE_REPO,
)

DIGEST_REF = re.compile(r'^(?P<repo>[a-z0-9][a-z0-9._/-]*)@(?P<digest>sha256:[0-9a-f]{64})$')


class ChannelError(Exception):
    """The channel could not be fetched, did not verify, or is malformed. Never run anything from it."""


@dataclass(frozen=True)
class Channel:
    agent: str  # entrius/gt-agent@sha256:...
    runner: str  # entrius/gt-agent-runner@sha256:...
    version: str
    issued_at: int

    @property
    def agent_digest(self) -> str:
        return DIGEST_REF.match(self.agent)['digest']


def allowed_signers_line(pubkey: str = RELEASE_PUBKEY_OPENSSH, namespace: str = RELEASE_SIGN_NAMESPACE) -> str:
    """The one line of the ``allowed_signers`` file ``ssh-keygen -Y verify`` reads: identity, namespace, key."""
    key = ' '.join(pubkey.split()[:2])  # type + base64; drop any comment
    return f'{RELEASE_SIGNER_IDENTITY} namespaces="{namespace}" {key}\n'


def fetch(url: str, timeout: float = CHANNEL_FETCH_TIMEOUT_S, opener: Callable = urllib.request.urlopen) -> bytes:
    try:
        with opener(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:  # any transport failure: the caller keeps what it has
        raise ChannelError(f'fetch {url}: {type(e).__name__}: {e}') from e


def verify(
    payload: bytes,
    signature: bytes,
    pubkey: str = RELEASE_PUBKEY_OPENSSH,
    run=subprocess.run,
    namespace: str = RELEASE_SIGN_NAMESPACE,
) -> None:
    """Raise ``ChannelError`` unless ``signature`` is a valid OpenSSH signature over ``payload`` by ``pubkey`` in
    ``namespace``: a signature made for another purpose (the channel vs a registry entry) does not verify."""
    if not pubkey.strip():
        raise ChannelError('no release public key compiled in (gittensor/agent/config.py RELEASE_PUBKEY_OPENSSH)')
    with tempfile.TemporaryDirectory(prefix='gt-channel-') as tmp:
        signers = Path(tmp) / 'allowed_signers'
        signers.write_text(allowed_signers_line(pubkey, namespace))
        sig = Path(tmp) / 'channel.sig'
        sig.write_bytes(signature)
        try:
            proc = run(
                [
                    'ssh-keygen',
                    '-Y',
                    'verify',
                    '-f',
                    str(signers),
                    '-I',
                    RELEASE_SIGNER_IDENTITY,
                    '-n',
                    namespace,
                    '-s',
                    str(sig),
                ],
                input=payload,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ChannelError(f'ssh-keygen unavailable: {e}') from e
    if proc.returncode != 0:
        raise ChannelError(
            f'signature does not verify: {(proc.stderr or proc.stdout).decode(errors="replace").strip()}'
        )


def parse(payload: bytes) -> Channel:
    try:
        doc = json.loads(payload)
    except ValueError as e:
        raise ChannelError(f'channel is not JSON: {e}') from e
    if not isinstance(doc, dict):
        raise ChannelError('channel is not a JSON object')
    try:
        agent, runner = str(doc['agent']), str(doc['runner'])
        version, issued_at = str(doc.get('version', '')), int(doc.get('issued_at', 0))
    except (KeyError, TypeError, ValueError) as e:
        raise ChannelError(f'channel is missing or mistypes a field: {e!r}') from e
    for ref, repo in ((agent, AGENT_IMAGE_REPO), (runner, RUNNER_IMAGE_REPO)):
        m = DIGEST_REF.match(ref)
        if not m or m['repo'] != repo:
            raise ChannelError(f'{ref!r} is not a digest-pinned {repo} reference')
    return Channel(agent, runner, version, issued_at)


def load(url: str, pubkey: str = RELEASE_PUBKEY_OPENSSH, opener: Callable = urllib.request.urlopen) -> Channel:
    """Fetch ``url`` and ``url + '.sig'``, verify, parse. Raises ``ChannelError`` on any failure."""
    payload = fetch(url, opener=opener)
    signature = fetch(url + '.sig', opener=opener)
    verify(payload, signature, pubkey)
    return parse(payload)
