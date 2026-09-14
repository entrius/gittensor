# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Prerequisite checks for `gitt up`: driver, docker, NVIDIA toolkit, a free SSH port, hotkey on disk, hotkey on chain.

Every probe of the host goes through :class:`HostProbe` so the checks are unit-testable with a fake; nothing in
this module imports ``bittensor`` at module load (the chain lookup imports it lazily inside the probe).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rich.table import Table

from gittensor.agent.config import AGENT_CONTAINER_NAME, RUNNER_CONTAINER_NAME

BLESSED_GPU_MARKER = '5090'  # the only card the pool blesses today (vault 24 §5: multi-type is later)
DEFAULT_WALLET_PATH = Path.home() / '.bittensor' / 'wallets'


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool | None  # None = skipped
    detail: str
    required: bool = True

    @property
    def status(self) -> str:
        if self.ok is None:
            return 'skip'
        if self.ok:
            return 'pass'
        return 'fail' if self.required else 'warn'

    def as_dict(self) -> dict:
        return {'name': self.name, 'status': self.status, 'detail': self.detail}


@dataclass
class PrereqReport:
    results: list[CheckResult] = field(default_factory=list)
    hotkey_ss58: str | None = None
    agent_state: str | None = None  # docker container status, None when no such container
    runner_state: str | None = None

    @property
    def ok(self) -> bool:
        return all(r.ok is not False for r in self.results if r.required)

    @property
    def already_up(self) -> bool:
        return 'running' in (self.agent_state, self.runner_state)


class HostProbe:
    """Real host access. Tests replace it with an object exposing the same methods."""

    def run(self, cmd: Sequence[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return subprocess.CompletedProcess(list(cmd), 127, '', f'{cmd[0]}: command not found')
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(list(cmd), 124, '', f'{cmd[0]}: timed out after {timeout:g}s')

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('0.0.0.0', port))
            except OSError:
                return False
        return True

    def hotkey_ss58(self, wallet: str, hotkey: str, wallet_path: Path = DEFAULT_WALLET_PATH) -> str | None:
        """The hotkey's ss58 from the wallet file on disk (public field only; no bittensor import)."""
        path = Path(wallet_path).expanduser() / wallet / 'hotkeys' / hotkey
        try:
            return json.loads(path.read_text()).get('ss58Address') or None
        except (OSError, ValueError, AttributeError):
            return None

    def is_registered(self, ss58: str, netuid: int, endpoint: str) -> bool:
        import bittensor as bt

        return bool(bt.Subtensor(network=endpoint).is_hotkey_registered(hotkey_ss58=ss58, netuid=netuid))

    def container_state(self, name: str) -> str | None:
        proc = self.run(['docker', 'inspect', '--format', '{{.State.Status}}', name])
        return proc.stdout.strip() or None if proc.returncode == 0 else None


# --- individual checks -------------------------------------------------------------------------------------------


def check_driver(probe: HostProbe) -> list[CheckResult]:
    proc = probe.run(['nvidia-smi', '--query-gpu=name,driver_version,uuid', '--format=csv,noheader'])
    if proc.returncode != 0:
        return [CheckResult('NVIDIA driver', False, (proc.stderr or proc.stdout).strip()[:120] or 'nvidia-smi failed')]
    rows = [[c.strip() for c in line.split(',')] for line in proc.stdout.splitlines() if line.strip()]
    if not rows:
        return [CheckResult('NVIDIA driver', False, 'nvidia-smi reports no GPUs')]
    names = [r[0] for r in rows]
    driver = rows[0][1] if len(rows[0]) > 1 else '?'
    results = [CheckResult('NVIDIA driver', True, f'{driver}; {len(rows)} GPU(s): {", ".join(names)}')]
    if not all(BLESSED_GPU_MARKER in n for n in names):
        results.append(
            CheckResult(
                'GPU model',
                False,
                f'pool blesses RTX {BLESSED_GPU_MARKER} only; found {", ".join(names)}',
                required=False,
            )
        )
    return results


def check_docker(probe: HostProbe) -> CheckResult:
    proc = probe.run(['docker', 'info', '--format', '{{.ServerVersion}}'])
    if proc.returncode != 0:
        return CheckResult('Docker daemon', False, (proc.stderr or proc.stdout).strip()[:120] or 'docker info failed')
    return CheckResult('Docker daemon', True, f'server {proc.stdout.strip()}')


def check_toolkit(probe: HostProbe) -> CheckResult:
    for binary in ('nvidia-ctk', 'nvidia-container-cli', 'nvidia-container-runtime'):
        if probe.which(binary):
            return CheckResult('NVIDIA container toolkit', True, f'{binary} on PATH')
    proc = probe.run(['docker', 'info', '--format', '{{json .Runtimes}}'])
    if proc.returncode == 0 and '"nvidia"' in proc.stdout:
        return CheckResult('NVIDIA container toolkit', True, 'nvidia runtime registered with docker')
    return CheckResult(
        'NVIDIA container toolkit', False, 'nvidia-ctk / nvidia-container-cli not found and no nvidia docker runtime'
    )


def check_ports(probe: HostProbe, ports: Sequence[int], report: PrereqReport) -> CheckResult:
    if report.already_up:
        return CheckResult(
            'Ports free', True, f'{", ".join(map(str, ports))} held by {AGENT_CONTAINER_NAME} (already up)'
        )
    busy = [p for p in ports if not probe.port_free(p)]
    if busy:
        return CheckResult('Ports free', False, f'in use: {", ".join(map(str, busy))} (pick --ssh-port)')
    return CheckResult('Ports free', True, ', '.join(map(str, ports)))


def check_hotkey(probe: HostProbe, wallet: str, hotkey: str) -> tuple[CheckResult, str | None]:
    ss58 = probe.hotkey_ss58(wallet, hotkey)
    if not ss58:
        return CheckResult(
            'Wallet hotkey', False, f'no readable hotkey at ~/.bittensor/wallets/{wallet}/hotkeys/{hotkey}'
        ), None
    return CheckResult('Wallet hotkey', True, f'{wallet}/{hotkey} = {ss58[:8]}...{ss58[-6:]}'), ss58


def check_registered(probe: HostProbe, ss58: str | None, netuid: int, endpoint: str, skip: bool) -> CheckResult:
    name = f'Registered on netuid {netuid}'
    if not ss58:
        return CheckResult(name, False, 'no hotkey to look up')
    if skip:
        return CheckResult(name, None, 'skipped (--dry-run)')
    try:
        registered = probe.is_registered(ss58, netuid, endpoint)
    except Exception as e:  # network / RPC trouble: report, do not crash the table
        return CheckResult(name, False, f'lookup failed against {endpoint}: {e}'[:160])
    if not registered:
        return CheckResult(name, False, f'hotkey not registered on netuid {netuid} ({endpoint})')
    return CheckResult(name, True, endpoint)


# --- the run -----------------------------------------------------------------------------------------------------


def run_prereqs(
    probe: HostProbe,
    *,
    wallet: str,
    hotkey: str,
    netuid: int,
    endpoint: str,
    ssh_port: int,
    skip_chain: bool = False,
    no_chain: bool = False,
) -> PrereqReport:
    """``no_chain`` is for our own dev boxes only: no registration lookup, and no hotkey needed on disk."""
    report = PrereqReport()
    report.results.extend(check_driver(probe))
    docker = check_docker(probe)
    report.results.append(docker)
    report.results.append(check_toolkit(probe))
    if docker.ok:
        report.agent_state = probe.container_state(AGENT_CONTAINER_NAME)
        report.runner_state = probe.container_state(RUNNER_CONTAINER_NAME)
    report.results.append(check_ports(probe, [ssh_port], report))
    hotkey_result, report.hotkey_ss58 = check_hotkey(probe, wallet, hotkey)
    if no_chain and not hotkey_result.ok:
        hotkey_result = CheckResult(hotkey_result.name, None, 'none on disk (--no-chain dev box)')
    report.results.append(hotkey_result)
    if no_chain:
        report.results.append(
            CheckResult(
                f'Registered on netuid {netuid}', None, 'skipped (--no-chain: a dev box, NOT a registered miner)'
            )
        )
    else:
        report.results.append(check_registered(probe, report.hotkey_ss58, netuid, endpoint, skip_chain))
    return report


_STATUS_MARKUP = {
    'pass': '[green]✓ pass[/green]',
    'fail': '[red]✗ fail[/red]',
    'warn': '[yellow]! warn[/yellow]',
    'skip': '[dim]— skip[/dim]',
}


def render_table(results: Sequence[CheckResult]) -> Table:
    table = Table(title='gitt up — prerequisites', show_header=True)
    table.add_column('Check', style='cyan', no_wrap=True)
    table.add_column('Status', no_wrap=True)
    table.add_column('Detail', style='dim')
    for r in results:
        table.add_row(r.name, _STATUS_MARKUP[r.status], r.detail)
    return table
