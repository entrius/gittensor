# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``SshRunner``: the real ``HostRunner`` — run one command on a miner box as root over SSH with a per-visit
certificate, against the host key pinned at ADMIT.

Every ``run`` is one ``ssh`` process. The credential is minted on first use and re-minted when it nears expiry, so a
long visit (a lease start that waits on a model load, a fleet-wide probe) never fails on a stale certificate. A
transport failure (no route, refused, host key mismatch, certificate refused: ssh exit 255) raises
``SshTransportError``; a command that ran and failed returns its exit code like any ``CommandResult``.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from gittensor.agent.config import AGENT_SSH_PORT, CERT_PRINCIPAL, CERT_VALIDITY_S
from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.ssh.certs import CertificateAuthority, VisitCredential

SSH_EXIT_TRANSPORT = 255
CONNECT_TIMEOUT_S = 10


class SshTransportError(Exception):
    """ssh itself failed (exit 255): the box is unreachable or refused us. The command did not run."""


def known_hosts_line(host: str, port: int, host_key: str) -> str:
    """One ``known_hosts`` entry for the box, ``host_key`` being the ``ssh-ed25519 AAAA...`` line pinned at ADMIT."""
    key = ' '.join(host_key.split()[:2])
    return f'[{host}]:{port} {key}\n'


def scan_host_key(
    host: str, port: int = AGENT_SSH_PORT, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> str:
    """The box's ed25519 host key as ``ssh-keyscan`` reports it, for pinning at ADMIT (trust on first use, once;
    afterwards a changed host key is a failed visit, never a re-scan)."""
    proc = run(
        ['ssh-keyscan', '-t', 'ed25519', '-T', str(CONNECT_TIMEOUT_S), '-p', str(port), host],
        capture_output=True,
        text=True,
        timeout=CONNECT_TIMEOUT_S + 5,
    )
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == 'ssh-ed25519':
            return f'{parts[1]} {parts[2]}'
    raise SshTransportError(f'ssh-keyscan {host}:{port}: no ed25519 host key ({proc.stderr.strip()[:200]})')


class SshRunner:
    """``HostRunner`` over OpenSSH. Use as a context manager so the visit credential is discarded on exit."""

    def __init__(
        self,
        host: str,
        port: int,
        ca: CertificateAuthority,
        known_hosts: str | Path,
        key_id: str,
        *,
        user: str = CERT_PRINCIPAL,
        validity_s: int = CERT_VALIDITY_S,
        source_address: str | None = None,
        ssh: str = 'ssh',
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        self.host, self.port, self.user = host, port, user
        self.ca, self.known_hosts, self.key_id = ca, Path(known_hosts), key_id
        self.validity_s, self.source_address = validity_s, source_address
        self._ssh, self._run, self._clock = ssh, run, clock
        self._credential: VisitCredential | None = None
        self.minted = 0

    # -- credential -----------------------------------------------------------------------------------------------

    def credential(self) -> VisitCredential:
        """The current certificate, minted or refreshed as needed."""
        if self._credential is None or self._credential.expired(self._clock()):
            if self._credential is not None:
                self._credential.discard()
            self._credential = self.ca.mint(self.key_id, self.user, self.validity_s, source_address=self.source_address)
            self.minted += 1
        return self._credential

    def close(self) -> None:
        if self._credential is not None:
            self._credential.discard()
            self._credential = None

    def __enter__(self) -> SshRunner:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- commands -------------------------------------------------------------------------------------------------

    def ssh_argv(self, credential: VisitCredential, command: str) -> list[str]:
        return [
            self._ssh,
            '-i',
            str(credential.key_path),
            '-o',
            f'CertificateFile={credential.cert_path}',
            '-o',
            'IdentitiesOnly=yes',
            '-o',
            'BatchMode=yes',
            '-o',
            'StrictHostKeyChecking=yes',
            '-o',
            f'UserKnownHostsFile={self.known_hosts}',
            '-o',
            f'ConnectTimeout={CONNECT_TIMEOUT_S}',
            '-o',
            'ServerAliveInterval=15',
            '-p',
            str(self.port),
            f'{self.user}@{self.host}',
            '--',
            command,
        ]

    def run(self, command: str, timeout: float | None = None, stdin: bytes | None = None) -> CommandResult:
        argv = self.ssh_argv(self.credential(), command)
        try:
            proc = self._run(argv, input=stdin, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise SshTransportError(
                f'{self.host}:{self.port}: timed out after {timeout}s running {command[:80]!r}'
            ) from e
        except OSError as e:
            raise SshTransportError(f'{self._ssh}: {e}') from e
        stdout = _text(proc.stdout)
        stderr = _text(proc.stderr)
        if proc.returncode == SSH_EXIT_TRANSPORT:
            raise SshTransportError(f'{self.host}:{self.port}: {stderr.strip()[:300] or "ssh exit 255"}')
        return CommandResult(proc.returncode, stdout, stderr)

    def docker_host(self) -> str:
        """``DOCKER_HOST`` form for docker-py / the docker CLI's ssh transport, should a caller want it."""
        return f'ssh://{self.user}@{self.host}:{self.port}'

    def __repr__(self) -> str:
        return f'SshRunner({shlex.quote(self.user + "@" + self.host)}:{self.port}, key_id={self.key_id!r})'


def _text(data) -> str:
    if data is None:
        return ''
    return data.decode(errors='replace') if isinstance(data, bytes) else str(data)
