# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Prerequisite checks for `gitt up`: driver, docker, NVIDIA toolkit, a free SSH port and workload port range, the
public IP and whether the SSH port answers on it, hotkey on disk, hotkey on chain.

Every probe of the host goes through :class:`HostProbe` so the checks are unit-testable with a fake; nothing in
this module imports ``bittensor`` at module load (the chain lookups and the serve import it lazily inside the probe).
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rich.table import Table

from gittensor.agent.config import (
    AGENT_CONTAINER_NAME,
    COMPUTE_AXON_MARKER,
    COMPUTE_AXON_PROTOCOL,
    COMPUTE_AXON_SCHEMA,
    RUNNER_CONTAINER_NAME,
    WORKLOAD_PORT_RANGE,
    is_compute_axon,
)

BLESSED_GPU_MARKER = '5090'  # the only card the pool blesses today (vault 24 §5: multi-type is later)
DEFAULT_WALLET_PATH = Path.home() / '.bittensor' / 'wallets'
PUBLIC_IP_SERVICES = ('https://checkip.amazonaws.com', 'https://api.ipify.org')  # each answers the caller's IP, plain
PUBLIC_IP_TIMEOUT_S = 5.0
REACHABILITY_TIMEOUT_S = 3.0
WORKLOAD_PORTS = range(WORKLOAD_PORT_RANGE[0], WORKLOAD_PORT_RANGE[1] + 1)


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
    public_ip: str | None = None  # what `gitt up` publishes on chain; None when it has none
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

    def public_ip(self) -> str | None:
        """This box's address as the internet sees it, from the first echo service that answers."""
        for url in PUBLIC_IP_SERVICES:
            try:
                with urllib.request.urlopen(url, timeout=PUBLIC_IP_TIMEOUT_S) as response:
                    text = response.read(64).decode().strip()
                return str(ipaddress.ip_address(text))
            except (OSError, ValueError):
                continue
        return None

    def reachable(self, ip: str, port: int, timeout: float = REACHABILITY_TIMEOUT_S) -> bool:
        """Best effort, from the box itself: connect to ``ip:port``, with a listener of our own on the port while
        nothing else holds it. A NAT without hairpin fails this and is still reachable from outside."""
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        listener = None
        try:
            if self.port_free(port):
                listener = socket.create_server(('', port), family=family)
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False
        finally:
            if listener is not None:
                listener.close()

    def chain_endpoint(self, ss58: str, netuid: int, endpoint: str) -> tuple[str, int, bool] | None:
        """What the chain holds for the hotkey now: ``(ip, port, carries the compute marker)``, None when not serving."""
        import bittensor as bt

        neuron = bt.Subtensor(network=endpoint).get_neuron_for_pubkey_and_subnet(ss58, netuid=netuid)
        axon = None if neuron is None or neuron.is_null else neuron.axon_info
        if axon is None or not axon.is_serving:
            return None
        return str(axon.ip), int(axon.port), is_compute_axon(axon.protocol, axon.placeholder1, axon.placeholder2)

    def serve(self, wallet: str, hotkey: str, netuid: int, endpoint: str, ip: str, port: int) -> str:
        """Serve ``ip:port`` with the compute marker, signed by the miner's hotkey. Returns '' or the failure."""
        import bittensor as bt
        from bittensor.core.extrinsics.serving import serve_extrinsic

        response = serve_extrinsic(
            subtensor=bt.Subtensor(network=endpoint),
            wallet=bt.Wallet(name=wallet, hotkey=hotkey),
            ip=ip,
            port=port,
            protocol=COMPUTE_AXON_PROTOCOL,
            netuid=netuid,
            placeholder1=COMPUTE_AXON_MARKER,
            placeholder2=COMPUTE_AXON_SCHEMA,
            mev_protection=False,  # nothing to front-run in an axon
        )
        return '' if response.success else str(response.message or 'serve_axon failed')

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


def check_workload_ports(probe: HostProbe, ports: range, report: PrereqReport) -> CheckResult:
    name = 'Workload ports'
    span = f'{ports[0]}-{ports[-1]}'
    busy = [p for p in ports if not probe.port_free(p)]
    if busy and report.already_up:
        return CheckResult(name, True, f'{span}: {len(busy)} in use (instances already placed here)')
    if busy:
        return CheckResult(
            name, False, f'{span} in use: {", ".join(map(str, busy))} (the controller places instances on these)'
        )
    return CheckResult(name, True, f'{span} free (open them to the internet, like the sshd port)')


def check_public_ip(probe: HostProbe, given: str | None) -> tuple[CheckResult, str | None]:
    name = 'Public IP'
    if given:
        try:
            ip, source = str(ipaddress.ip_address(given.strip())), '--ip'
        except ValueError:
            return CheckResult(name, False, f'--ip {given!r} is not an IP address'), None
    else:
        ip, source = probe.public_ip(), 'detected'
        if not ip:
            return CheckResult(name, False, "could not detect this box's public IP (pass --ip)"), None
    if not ipaddress.ip_address(ip).is_global:
        return CheckResult(name, False, f'{ip} ({source}) is not public: the controller only dials public addresses'), None  # fmt: skip
    return CheckResult(name, True, f'{ip} ({source})'), ip


def check_reachable(probe: HostProbe, ip: str | None, port: int, skip: bool) -> CheckResult:
    """Best effort and never blocking: from inside, a NAT that does not hairpin looks the same as a closed port."""
    name = 'SSH port reachable'
    if skip:
        return CheckResult(name, None, 'skipped (--skip-reachability)', required=False)
    if not ip:
        return CheckResult(name, None, 'no public IP to try', required=False)
    if probe.reachable(ip, port):
        return CheckResult(name, True, f'{ip}:{port} answers', required=False)
    return CheckResult(
        name,
        False,
        f'{ip}:{port} did not answer from this box: open / forward it. Behind a NAT that does not hairpin this fails '
        f'anyway; check from another machine (nc -vz {ip} {port})',
        required=False,
    )


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
    public_ip: str | None = None,
    skip_reachability: bool = False,
    workload_ports: range = WORKLOAD_PORTS,
) -> PrereqReport:
    """``no_chain`` is for our own dev boxes only: no registration lookup, nothing published (so no public IP or
    reachability rows), and no hotkey needed on disk. ``public_ip`` overrides detection."""
    report = PrereqReport()
    report.results.extend(check_driver(probe))
    docker = check_docker(probe)
    report.results.append(docker)
    report.results.append(check_toolkit(probe))
    if docker.ok:
        report.agent_state = probe.container_state(AGENT_CONTAINER_NAME)
        report.runner_state = probe.container_state(RUNNER_CONTAINER_NAME)
    report.results.append(check_ports(probe, [ssh_port], report))
    report.results.append(check_workload_ports(probe, workload_ports, report))
    if no_chain:
        report.results.append(CheckResult('Public IP', None, 'skipped (--no-chain: nothing published)'))
    else:
        ip_result, report.public_ip = check_public_ip(probe, public_ip)
        report.results.append(ip_result)
        report.results.append(check_reachable(probe, report.public_ip, ssh_port, skip_reachability))
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


def run_publish_prereqs(
    probe: HostProbe,
    *,
    wallet: str,
    hotkey: str,
    netuid: int,
    endpoint: str,
    public_ip: str | None,
    ssh_port: int,
    skip_chain: bool = False,
    skip_reachability: bool = False,
) -> PrereqReport:
    """``gitt up --publish-only``: the wallet is here and the box is elsewhere, so only the rows the chain step needs.
    No driver, docker or port rows (they describe the box, not this machine); the reachability row dials the box from
    here, which is the controller's view of it."""
    report = PrereqReport()
    ip_result, report.public_ip = check_public_ip(probe, public_ip)
    report.results.append(ip_result)
    report.results.append(check_reachable(probe, report.public_ip, ssh_port, skip_reachability))
    hotkey_result, report.hotkey_ss58 = check_hotkey(probe, wallet, hotkey)
    report.results.append(hotkey_result)
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
