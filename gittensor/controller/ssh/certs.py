# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Per-visit SSH certificates (vault ``26`` §5).

The agent's sshd trusts one CA public key (``TrustedUserCAKeys``). For every visit the controller makes a fresh
ed25519 key, signs a certificate for it with the CA key (principal ``root``, valid a few minutes, optionally bound
to our source address) and logs in with the pair. Nothing is installed on the box and nothing needs cleaning up
there; the certificate expires. ``ssh-keygen`` does the crypto — no Python SSH library.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gittensor.agent.config import CERT_BACKDATE_S, CERT_PRINCIPAL, CERT_VALIDITY_S

_KEY_ID = re.compile(r'^[A-Za-z0-9_.:@=-]{1,128}$')


class CertificateError(Exception):
    """``ssh-keygen`` refused or is missing; no credential was produced."""


@dataclass(frozen=True)
class VisitCredential:
    """A private key + certificate pair for one visit. Lives in its own temp dir; :meth:`discard` removes it."""

    key_path: Path
    cert_path: Path
    key_id: str
    principal: str
    not_before: float
    not_after: float

    @property
    def workdir(self) -> Path:
        return self.key_path.parent

    def expired(self, now: float | None = None, margin_s: float = 30.0) -> bool:
        """True when a login started now (plus ``margin_s`` of connection setup) would be refused."""
        return (time.time() if now is None else now) + margin_s >= self.not_after

    def discard(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


class CertificateAuthority:
    """Mints certificates with the CA private key at ``ca_key_path`` (the controller container's secret)."""

    def __init__(
        self,
        ca_key_path: str | Path,
        ssh_keygen: str = 'ssh-keygen',
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        self.ca_key_path = Path(ca_key_path)
        self.ssh_keygen = ssh_keygen
        self._run = run
        self._clock = clock

    def mint(
        self,
        key_id: str,
        principal: str = CERT_PRINCIPAL,
        validity_s: int = CERT_VALIDITY_S,
        source_address: str | None = None,
    ) -> VisitCredential:
        """A fresh key + certificate. ``key_id`` is what sshd logs on the box (make it say who and why: e.g.
        ``ctl-heartbeat-<box>``); ``source_address`` (CIDR list) pins the certificate to our egress address."""
        if not _KEY_ID.match(key_id):
            raise CertificateError(f'key_id {key_id!r}: letters, digits and _.:@=- only, at most 128')
        if validity_s <= 0:
            raise CertificateError('validity must be positive')
        workdir = Path(tempfile.mkdtemp(prefix='gt-visit-'))
        key = workdir / 'id_ed25519'
        now = self._clock()
        try:
            self._keygen(['-q', '-t', 'ed25519', '-N', '', '-C', key_id, '-f', str(key)])
            args = [
                '-q',
                '-s',
                str(self.ca_key_path),
                '-I',
                key_id,
                '-n',
                principal,
                '-V',
                f'-{CERT_BACKDATE_S}s:+{validity_s}s',
            ]
            if source_address:
                args += ['-O', f'source-address={source_address}']
            self._keygen([*args, f'{key}.pub'])
        except CertificateError:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        cert = workdir / 'id_ed25519-cert.pub'
        if not cert.is_file():
            shutil.rmtree(workdir, ignore_errors=True)
            raise CertificateError('ssh-keygen produced no certificate')
        return VisitCredential(key, cert, key_id, principal, now - CERT_BACKDATE_S, now + validity_s)

    def _keygen(self, args: list[str]) -> None:
        try:
            proc = self._run([self.ssh_keygen, *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CertificateError(f'{self.ssh_keygen}: {e}') from e
        if proc.returncode != 0:
            raise CertificateError(f'{self.ssh_keygen} {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}')


def cert_details(cert_path: str | Path, ssh_keygen: str = 'ssh-keygen') -> dict[str, str]:
    """``ssh-keygen -L`` parsed into a flat dict (``Key ID``, ``Principals``, ``Valid``, ``Critical Options`` ...);
    list sections are joined with spaces. For tests and for a human reading a box's auth log."""
    proc = subprocess.run([ssh_keygen, '-L', '-f', str(cert_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise CertificateError(proc.stderr.strip()[:300])
    out: dict[str, str] = {}
    current = ''
    for line in proc.stdout.splitlines()[1:]:  # first line is the file name
        indent = len(line) - len(line.lstrip(' '))
        stripped = line.strip()
        if not stripped:
            continue
        if indent >= 16 and current:  # an item of the current list section
            out[current] = f'{out[current]} {stripped}'.strip()
            continue
        key, _, value = stripped.partition(':')
        current = key.strip()
        out[current] = value.strip()
    return out
