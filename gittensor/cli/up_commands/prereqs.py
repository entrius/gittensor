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
from gittensor.agent.launch import Workload, parse_workloads, workload_list_command
from gittensor.controller.checks.scrape import (
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    parse_kernel_driver,
    parse_md5,
)

BLESSED_GPU_MARKER = '5090'  # the only card the pool blesses today (vault 24 §5: multi-type is later)
DEFAULT_WALLET_PATH = Path.home() / '.bittensor' / 'wallets'
PUBLIC_IP_SERVICES = ('https://checkip.amazonaws.com', 'https://api.ipify.org')  # each answers the caller's IP, plain
PUBLIC_IP_TIMEOUT_S = 5.0
REACHABILITY_TIMEOUT_S = 3.0
# The vetted drivers, as the controller's full check judges them (``nvml_digest``): driver version -> the md5s of a
# genuine libnvidia-ml.so.1. Published with the code so a miner sees the answer here, before joining, and not as a bench.
NVML_ALLOWLIST_URL = 'https://raw.githubusercontent.com/entrius/gittensor/main/docker/controller/nvml_allowlist.json'
NVML_ALLOWLIST_TIMEOUT_S = 8.0
DRIVER_VETTED_CHECK = 'Driver vetted'
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
    workloads: list[Workload] = field(default_factory=list)  # the controller's gt-i-* containers present on the box

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

    def nvml_allowlist(self, url: str = NVML_ALLOWLIST_URL) -> dict[str, list[str]] | None:
        """The published driver allowlist, or None when it cannot be fetched (the check is then skipped, not failed)."""
        try:
            with urllib.request.urlopen(url, timeout=NVML_ALLOWLIST_TIMEOUT_S) as response:  # noqa: S310 (our own URL)
                data = json.loads(response.read().decode())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return {str(k): ([v] if isinstance(v, str) else [str(m) for m in v]) for k, v in data.items()}

    def nvml_md5(self) -> str:
        """md5 of the host's libnvidia-ml.so.1, found and hashed exactly as the controller does; '' when not found."""
        proc = self.run(['sh', '-c', NVML_MD5_COMMAND])
        return parse_md5(proc.stdout) if proc.returncode == 0 else ''

    def kernel_driver(self) -> str:
        """The loaded kernel module's version (/proc/driver/nvidia/version); '' when it cannot be read."""
        proc = self.run(['sh', '-c', KERNEL_DRIVER_COMMAND])
        return parse_kernel_driver(proc.stdout) if proc.returncode == 0 else ''

    def container_state(self, name: str) -> str | None:
        proc = self.run(['docker', 'inspect', '--format', '{{.State.Status}}', name])
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def workload_containers(self) -> list[Workload]:
        proc = self.run(workload_list_command())
        return parse_workloads(proc.stdout) if proc.returncode == 0 else []


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
    results.append(check_driver_vetted(probe, driver))
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


def check_driver_vetted(probe: HostProbe, driver: str) -> CheckResult:
    """The controller's ``nvml_digest`` check, run here first: an unknown driver, a driver upgraded without a reboot or
    a library that is not NVIDIA's own build benches the box for an hour or more once it has joined."""
    allowlist = probe.nvml_allowlist()
    if allowlist is None:
        return CheckResult(DRIVER_VETTED_CHECK, None, 'could not fetch the driver list; the controller checks it later')
    kernel = probe.kernel_driver()
    if kernel and kernel != driver:
        return CheckResult(
            DRIVER_VETTED_CHECK,
            False,
            f'nvidia-smi says {driver}, the loaded kernel module is {kernel}: reboot after a driver upgrade',
        )
    expected = allowlist.get(driver)
    if expected is None:
        newest = ', '.join(sorted(allowlist, key=_version_key)[-3:])
        return CheckResult(
            DRIVER_VETTED_CHECK,
            False,
            f'driver {driver} is not on the vetted list ({len(allowlist)} versions, newest {newest}): install a '
            'listed one, or ask in the Discord to have yours vetted. Joining with it gets the box benched',
        )
    md5 = probe.nvml_md5()
    if not md5:
        return CheckResult(DRIVER_VETTED_CHECK, False, 'libnvidia-ml.so.1 not found under /usr or /lib', required=False)
    if md5 not in {m.lower() for m in expected}:
        return CheckResult(
            DRIVER_VETTED_CHECK,
            False,
            f"libnvidia-ml.so.1 ({md5}) is not NVIDIA's build for driver {driver}: reinstall the driver from NVIDIA's "
            'package',
        )
    return CheckResult(DRIVER_VETTED_CHECK, True, f'{driver}: library matches the vetted build')


def _version_key(version: str) -> list[int]:
    return [int(part) if part.isdigit() else 0 for part in version.split('.')]


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


def check_workload_ports(probe: HostProbe, ports: range, report: PrereqReport, reclaim: bool = False) -> CheckResult:
    """A port held by one of our own gt-i-* containers (an orphan of a previous `gitt up`, found 9/16) is never a
    refusal: it is named, and removed with --reclaim; a port held by anything else fails the check."""
    name = 'Workload ports'
    span = f'{ports[0]}-{ports[-1]}'
    busy = [p for p in ports if not probe.port_free(p)]
    if busy and report.already_up:
        return CheckResult(name, True, f'{span}: {len(busy)} in use (instances already placed here)')
    ours = {w.port: w for w in report.workloads if w.port is not None}
    foreign = [p for p in busy if p not in ours]
    if foreign:
        return CheckResult(
            name, False, f'{span} in use: {", ".join(map(str, foreign))} (the controller places instances on these)'
        )
    if busy:
        held = ', '.join(f'{p} by {ours[p].name}' for p in busy)
        if reclaim:
            return CheckResult(name, True, f'{span}: {held}: our own workload(s), removed by --reclaim')
        return CheckResult(  # a warning, not a refusal: ours, so the agent may start beside it
            name,
            False,
            f'{span}: {held}: our own workload(s) left behind; the controller re-adopts or removes them, '
            '`gitt up --reclaim` removes them now',
            required=False,
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
    reclaim: bool = False,
    agent_only: bool = False,
) -> PrereqReport:
    """``no_chain`` is for our own dev boxes only: no registration lookup, nothing published (so no public IP or
    reachability rows), and no hotkey needed on disk. ``public_ip`` overrides detection. ``reclaim``: a workload
    container of ours left behind will be removed, so a port it holds passes. ``agent_only`` is the box half of
    ``--publish-only``: the wallet lives on another machine, so no hotkey is needed here and nothing is looked up or
    published, but the public IP and reachability rows still run (they are what the wallet machine publishes)."""
    report = PrereqReport()
    report.results.extend(check_driver(probe))
    docker = check_docker(probe)
    report.results.append(docker)
    report.results.append(check_toolkit(probe))
    if docker.ok:
        report.agent_state = probe.container_state(AGENT_CONTAINER_NAME)
        report.runner_state = probe.container_state(RUNNER_CONTAINER_NAME)
        report.workloads = probe.workload_containers()
    report.results.append(check_ports(probe, [ssh_port], report))
    report.results.append(check_workload_ports(probe, workload_ports, report, reclaim))
    if no_chain:
        report.results.append(CheckResult('Public IP', None, 'skipped (--no-chain: nothing published)'))
    else:
        ip_result, report.public_ip = check_public_ip(probe, public_ip)
        report.results.append(ip_result)
        report.results.append(check_reachable(probe, report.public_ip, ssh_port, skip_reachability))
    hotkey_result, report.hotkey_ss58 = check_hotkey(probe, wallet, hotkey)
    if no_chain and not hotkey_result.ok:
        hotkey_result = CheckResult(hotkey_result.name, None, 'none on disk (--no-chain dev box)')
    if agent_only:
        hotkey_result = CheckResult(hotkey_result.name, None, 'not needed here (--agent-only: the wallet is elsewhere)')
        report.hotkey_ss58 = None
    report.results.append(hotkey_result)
    if agent_only:
        report.results.append(
            CheckResult(f'Registered on netuid {netuid}', None, 'skipped (--agent-only: checked by --publish-only)')
        )
    elif no_chain:
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
