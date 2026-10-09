# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Prerequisite checks for `gitt up`: driver, docker, NVIDIA toolkit, a free SSH port and workload port range, the
public IP and whether the SSH port answers on it, hotkey on disk, hotkey on chain. With ``--rent`` (vault 29 §5): a
box size the pool admits, the Sysbox runtime, and a free rent port range.

Every probe of the host goes through :class:`HostProbe` so the checks are unit-testable with a fake; nothing in
this module imports ``bittensor`` at module load (the chain lookups and the serve import it lazily inside the probe).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
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
    RENT_PORTS_MIN,
    RENT_PORTS_MIN_DEV,
    RENTAL_LABEL,
    RUNNER_CONTAINER_NAME,
    SYSBOX_RUNTIME,
    SYSBOX_VERSION,
    WORKLOAD_PORT_RANGE,
    is_compute_axon,
)
from gittensor.agent.launch import Workload, parse_workloads, workload_list_command
from gittensor.controller.checks import config as ccfg
from gittensor.controller.checks.amd_scrape import AMD_SYSFS_COMMAND, AmdCard, AmdStack, parse_amd_sysfs, version_tuple
from gittensor.controller.checks.catalog import CardSpec, load_catalog, spec_for_name, spec_for_pci_id
from gittensor.controller.checks.scrape import (
    DOWNLOAD_PROBE_COMMAND,
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    parse_download_probe,
    parse_kernel_driver,
    parse_md5,
    parse_meminfo_total_gb,
)
from gittensor.controller.checks.vendor import (
    AMD,
    AMD_KFD,
    BOTH,
    NVIDIA,
    VENDOR_DETECT_COMMAND,
    parse_vendor,
    render_node_path,
    vendor_or_default,
)

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
SYSBOX_CHECK = 'Sysbox runtime'
VENDOR_CHECK = 'GPU vendor'
TOOLKIT_CHECK = 'NVIDIA container toolkit'
RENT_PORTS_CHECK = 'Rent ports'
HOST_RAM_CHECK = 'Host RAM'
CPU_THREADS_CHECK = 'CPU threads'
DOWNLOAD_CHECK = 'Download'
SYSBOX_SETUP_URL = 'https://raw.githubusercontent.com/entrius/gittensor/main/docker/agent/sysbox-setup.sh'
MINER_DOCS_URL = 'https://docs.gittensor.io/compute-mining.html'  # every row of the table, explained for the miner
SYSBOX_KERNEL_MIN = (5, 19)  # overlayfs over ID-mapped mounts; older kernels fall back to shiftfs (Lium's check)


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
    vendor: str = NVIDIA  # the host's GPU vendor as the controller's scrape would judge it (vault 30 §1 #2)

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

    def vendor_detected(self) -> str:
        """Which GPU kernel module is loaded, by the controller's own test: 'nvidia', 'amd', 'both' or ''."""
        proc = self.run(['sh', '-c', VENDOR_DETECT_COMMAND])
        return parse_vendor(proc.stdout) if proc.returncode == 0 else ''

    def amd_sysfs(self) -> tuple[list[AmdCard], AmdStack]:
        """The AMD cards and the stack as the controller's own scrape reads them (``amd_scrape``)."""
        proc = self.run(['sh', '-c', AMD_SYSFS_COMMAND])
        return parse_amd_sysfs(proc.stdout) if proc.returncode == 0 else ([], AmdStack())

    def container_state(self, name: str) -> str | None:
        proc = self.run(['docker', 'inspect', '--format', '{{.State.Status}}', name])
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def device_mode(self, path: str) -> int | None:
        """The permission bits of a device node (``0o666``), None when it is missing."""
        try:
            return os.stat(path).st_mode & 0o777
        except OSError:
            return None

    def kernel_release(self) -> str:
        proc = self.run(['uname', '-r'])
        return proc.stdout.strip() if proc.returncode == 0 else ''

    def meminfo(self) -> str:
        """/proc/meminfo as text ('' when unreadable): the controller's ``meminfo`` step, read here on the host itself."""
        try:
            return Path('/proc/meminfo').read_text()
        except OSError:
            return ''

    def cpu_threads(self) -> int | None:
        return os.cpu_count()

    def download_probe(self, timeout: float = ccfg.DOWNLOAD_PROBE_TIMEOUT_S + 15) -> str:
        """The controller's download probe (one real transfer, ``scrape.DOWNLOAD_PROBE_COMMAND``), run once here so
        the miner sees the number the round will see; '' when it did not run to the end."""
        proc = self.run(['sh', '-c', DOWNLOAD_PROBE_COMMAND], timeout=timeout)
        return proc.stdout if proc.returncode == 0 else ''

    def rental_ports(self) -> set[int]:
        """The host ports our customer pods (``RENTAL_LABEL``) hold on this box, from ``docker ps``."""
        proc = self.run(['docker', 'ps', '--filter', f'label={RENTAL_LABEL}', '--format', '{{.Ports}}'])
        if proc.returncode != 0:
            return set()
        found: set[int] = set()
        for match in re.finditer(r':(\d+)->', proc.stdout):
            found.add(int(match.group(1)))
        return found

    def workload_containers(self) -> list[Workload]:
        proc = self.run(workload_list_command())
        return parse_workloads(proc.stdout) if proc.returncode == 0 else []


# --- individual checks -------------------------------------------------------------------------------------------


def check_driver(probe: HostProbe) -> tuple[list[CheckResult], list[CardSpec | None]]:
    """The driver rows and, beside them, the catalog row of each card (None for a card the catalog does not know):
    what the host rows (``check_host``) scale their floors by."""
    proc = probe.run(['nvidia-smi', '--query-gpu=name,driver_version,uuid', '--format=csv,noheader'])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()[:120] or 'nvidia-smi failed'
        return [CheckResult('NVIDIA driver', False, f'{err}: install the NVIDIA driver (nvidia-smi must work), reboot, re-run')], []  # fmt: skip
    rows = [[c.strip() for c in line.split(',')] for line in proc.stdout.splitlines() if line.strip()]
    if not rows:
        return [
            CheckResult('NVIDIA driver', False, 'nvidia-smi reports no GPUs: no card the pool can admit on this box')
        ], []
    names = [r[0] for r in rows]
    driver = rows[0][1] if len(rows[0]) > 1 else '?'
    results = [CheckResult('NVIDIA driver', True, f'{driver}; {len(rows)} GPU(s): {", ".join(names)}')]
    results.append(check_driver_vetted(probe, driver))
    model = check_gpu_model(names)
    if model is not None:
        results.append(model)
    return results, [spec_for_name(n) for n in names]


def check_vendor(detected: str) -> CheckResult:
    """The controller's ``vendor`` check, said here first: one GPU vendor per box (vault 30 §3)."""
    if detected == BOTH:
        return CheckResult(
            VENDOR_CHECK, False, 'both the nvidia and the amdgpu kernel module are loaded: one vendor per box'
        )
    if detected == AMD:
        return CheckResult(VENDOR_CHECK, True, 'amdgpu (cards attach as device nodes, no container runtime)')
    return CheckResult(VENDOR_CHECK, True, 'nvidia' if detected == NVIDIA else 'no GPU kernel module found; checking nvidia-smi')  # fmt: skip


AMD_DRIVER_CHECK = 'AMD driver'
AMD_FLOOR_CHECK = 'AMD driver floor'
AMD_NODES_CHECK = 'AMD device nodes'
AMD_UDEV_RULE = '/etc/udev/rules.d/99-gittensor-amd.rules'
AMD_NODE_MODE = 0o666


def check_amd_driver(probe: HostProbe) -> tuple[list[CheckResult], list[CardSpec | None]]:
    """The AMD box's driver rows, the controller's ``gpu_spec`` and ``amd_stack`` rules said here first (vault 31
    step 4): every card with a usable serial and whole (SPX / NPS1), its type by PCI id, and the kernel or DKMS
    driver at the pool floor. Beside the rows, the catalog row of each card, as ``check_driver`` returns it."""
    cards, stack = probe.amd_sysfs()
    if not cards:
        return [CheckResult(AMD_DRIVER_CHECK, False, 'amdgpu is loaded but sysfs lists no AMD card: no card is usable')], []  # fmt: skip
    problems = []
    names = []
    specs: list[CardSpec | None] = []
    for c in cards:
        spec = spec_for_pci_id(c.device_id)
        specs.append(spec)
        names.append(spec.gpu_type if spec is not None else f'{c.device_id} (not in the GPU catalog)')
        if spec is None:  # the controller's gpu_spec check refuses it; the miner hears it here first
            problems.append(
                f'{c.render_node} is device {c.device_id}, not a type in the GPU catalog: the pool cannot admit it'
            )
        if not c.id_ok:
            problems.append(f'{c.render_node} reports no usable serial (unique_id): the pool cannot pin the card')
        if not c.whole:
            problems.append(f'{c.render_node} is partitioned ({c.partition}): the pool admits SPX / NPS1 only')
    if problems:
        return [CheckResult(AMD_DRIVER_CHECK, False, '; '.join(problems)[:200])], specs
    detail = f'amdgpu; {len(cards)} card(s): {", ".join(sorted(set(names)))}; {", ".join(c.render_node for c in cards)}'
    rows = [CheckResult(AMD_DRIVER_CHECK, True, detail)]
    # Measured 10/9 (vault 33): under Sysbox the nodes appear as nobody:nogroup inside the pod, so no group bit can
    # apply and --group-add is useless; the nodes must be world-readable-writable on the host. A stock udev rule
    # makes them 0660 root:render; docker/agent/sysbox-setup.sh sets 0666 and drops a rule that keeps it.
    nodes = [AMD_KFD, *(render_node_path(c.render_node) for c in cards)]
    wrong = [n for n in nodes if probe.device_mode(n) != AMD_NODE_MODE]
    if wrong:
        rows.append(
            CheckResult(
                AMD_NODES_CHECK,
                False,
                f'{", ".join(wrong)} not 0666: a pod under Sysbox cannot open the card. Fix: chmod 0666 {" ".join(nodes)} '
                f'and write {AMD_UDEV_RULE} (docker/agent/sysbox-setup.sh does both)',
            )
        )
    else:
        rows.append(CheckResult(AMD_NODES_CHECK, True, f'{", ".join(nodes)} are 0666'))
    kmin, dmin = ccfg.AMD_KERNEL_MIN, ccfg.AMD_DKMS_MIN
    if version_tuple(stack.kernel) >= kmin:
        rows.append(CheckResult(AMD_FLOOR_CHECK, True, f'kernel {stack.kernel}'))
    elif stack.amdgpu and version_tuple(stack.amdgpu) >= dmin:
        rows.append(CheckResult(AMD_FLOOR_CHECK, True, f'amdgpu DKMS {stack.amdgpu} on kernel {stack.kernel}'))
    else:
        rows.append(
            CheckResult(
                AMD_FLOOR_CHECK,
                False,
                f'kernel {stack.kernel} is below {kmin[0]}.{kmin[1]} and no amdgpu DKMS driver at or above '
                f'{dmin[0]}.{dmin[1]} is loaded: update the kernel or install the ROCm driver',
            )
        )
    return rows, specs


def check_host(probe: HostProbe, specs: Sequence[CardSpec | None]) -> list[CheckResult]:
    """The controller's ``host_spec`` check (host specs, 10/9), said here first: host RAM and CPU threads against the
    per-card floors x this box's card count, and one run of the same download probe the round makes. The numbers
    are the controller's config, read from it, never copied. A shortfall is a warning that names the fix and says
    whether the pool advertises it (the customer sees what the box has) or refuses it (``HOST_SPEC_HARD``; the
    download floor is always refused, after ``DOWNLOAD_FAIL_AFTER`` rounds under it). The box still starts."""
    count = max(1, len(specs))
    hard = ccfg.HOST_SPEC_HARD
    fate = 'the controller refuses this box (host_spec)' if hard else 'advertised to customers, not refused'
    rows: list[CheckResult] = []

    def row(name: str, value: float | None, per_card: float, unit: str, what: str) -> CheckResult:
        floor = per_card * count
        need = f'{per_card:.0f} {unit} per card, {floor:.0f} {unit} for {count}'
        if value is None:
            return CheckResult(name, None, f'could not read the host {what}')
        if value >= floor:
            return CheckResult(name, True, f'{value:.0f} {unit} (floor {floor:.0f} {unit} for {count} card{"s" if count > 1 else ""})')  # fmt: skip
        return CheckResult(name, False, f'{value:.0f} {unit}; a box of this type needs {need}: {fate}', required=False)

    rows.append(row(HOST_RAM_CHECK, parse_meminfo_total_gb(probe.meminfo()), ccfg.RAM_MIN_GB_PER_GPU, 'GB', 'RAM'))
    rows.append(row(CPU_THREADS_CHECK, probe.cpu_threads(), ccfg.CPU_THREADS_MIN_PER_GPU, 'threads', 'CPU count'))
    mbps = parse_download_probe(probe.download_probe()).mbps
    floor, after = ccfg.DOWNLOAD_MIN_MBPS, ccfg.DOWNLOAD_FAIL_AFTER
    if mbps is None:
        rows.append(
            CheckResult(
                DOWNLOAD_CHECK,
                None,
                'could not measure (Docker Hub did not answer); the controller measures every round',
            )  # fmt: skip
        )
    elif mbps >= floor:
        pull = f'{ccfg.DOWNLOAD_PROBE_BYTES / 1e6:.0f} MB pull from Docker Hub'
        rows.append(
            CheckResult(
                DOWNLOAD_CHECK, True, f'{mbps:.0f} Mbps on a {pull} (floor {floor:.0f} Mbps, measured every round)'
            )  # fmt: skip
        )
    else:
        rows.append(
            CheckResult(
                DOWNLOAD_CHECK,
                False,
                f'{mbps:.0f} Mbps; the floor is {floor:.0f} Mbps: refused once the average over rounds is under it '
                f'for {after} rounds in a row',
                required=False,
            )
        )
    return rows


def check_gpu_model(names: Sequence[str]) -> CheckResult | None:
    """The controller's ``gpu_spec`` model rule, said here first: every card one type, a type the pool admits, and a
    box size the type admits (a rental takes the whole box, 29 §1 #3)."""
    specs = [spec_for_name(n) for n in names]
    admitted = ', '.join(sorted(t for t, s in load_catalog().items() if s.qualified))
    found = ', '.join(names)
    if any(s is None or not s.qualified for s in specs):
        return CheckResult(
            'GPU model', False, f'pool admits {admitted} only; found {found}: the controller will refuse this box (gpu_spec)', required=False
        )  # fmt: skip
    if len({s.gpu_type for s in specs if s is not None}) > 1:
        return CheckResult('GPU model', False, f'every card on a box must be one type; found {found}', required=False)
    spec = specs[0]
    if spec is not None and len(names) not in spec.counts:
        sizes = ', '.join(map(str, spec.counts))
        return CheckResult(
            'GPU model', False, f'{len(names)} cards: the pool admits {spec.gpu_type} boxes of {sizes}', required=False
        )
    return None


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
        err = (proc.stderr or proc.stdout).strip()[:120] or 'docker info failed'
        return CheckResult('Docker daemon', False, f'{err}: install Docker (docs.docker.com/engine/install) and run as a user in the docker group, or with sudo')  # fmt: skip
    return CheckResult('Docker daemon', True, f'server {proc.stdout.strip()}')


def check_toolkit(probe: HostProbe) -> CheckResult:
    for binary in ('nvidia-ctk', 'nvidia-container-cli', 'nvidia-container-runtime'):
        if probe.which(binary):
            return CheckResult(TOOLKIT_CHECK, True, f'{binary} on PATH')
    proc = probe.run(['docker', 'info', '--format', '{{json .Runtimes}}'])
    if proc.returncode == 0 and '"nvidia"' in proc.stdout:
        return CheckResult(TOOLKIT_CHECK, True, 'nvidia runtime registered with docker')
    return CheckResult(
        TOOLKIT_CHECK,
        False,
        'nvidia-ctk / nvidia-container-cli not found and no nvidia docker runtime: install the NVIDIA Container Toolkit '
        '(sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker)',
    )


def check_sysbox(probe: HostProbe) -> CheckResult:
    """``--rent``: every customer pod runs under Sysbox (``--runtime=sysbox-runc``), never ``--privileged``; root,
    docker and systemd work inside without host root (29 §1 #5). Lium's executors require the same."""
    proc = probe.run(['docker', 'info', '--format', '{{json .Runtimes}}'])
    if proc.returncode != 0:
        return CheckResult(SYSBOX_CHECK, False, (proc.stderr or proc.stdout).strip()[:120] or 'docker info failed')
    if f'"{SYSBOX_RUNTIME}"' not in proc.stdout:
        return CheckResult(
            SYSBOX_CHECK,
            False,
            f'{SYSBOX_RUNTIME} is not registered with docker: install Sysbox {SYSBOX_VERSION} '
            f'(curl -fsSL {SYSBOX_SETUP_URL} | sudo bash), then re-run',
        )
    kernel = probe.kernel_release()
    if kernel and _kernel_key(kernel) < SYSBOX_KERNEL_MIN:
        floor = '.'.join(map(str, SYSBOX_KERNEL_MIN))
        return CheckResult(
            SYSBOX_CHECK,
            False,
            f'{SYSBOX_RUNTIME} registered; kernel {kernel} is older than {floor}: pods may fail to start (Sysbox '
            'needs overlayfs over ID-mapped mounts)',
            required=False,
        )
    return CheckResult(
        SYSBOX_CHECK, True, f'{SYSBOX_RUNTIME} registered with docker' + (f'; kernel {kernel}' if kernel else '')
    )


def _kernel_key(release: str) -> tuple[int, int]:
    parts = release.split('-', 1)[0].split('.')
    return (int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0)


def check_rent_ports(
    probe: HostProbe,
    ports: tuple[int, int],
    ssh_port: int,
    report: PrereqReport,
    workload_ports: range = WORKLOAD_PORTS,
    minimum: int = RENT_PORTS_MIN,
) -> CheckResult:
    """``--rent``: the range a customer's pod publishes its ports on (29 §5). Wide enough, apart from the sshd and
    workload ports, and free on this box except for our own pods (a rental still running from a previous `gitt up`
    is the controller's to end, not a reason to refuse)."""
    low, high = ports
    span = f'{low}-{high}'
    width = high - low + 1
    if width < minimum:
        return CheckResult(RENT_PORTS_CHECK, False, f'{span} is {width} ports; --rent-ports needs at least {minimum}')
    if low <= ssh_port <= high or low <= workload_ports[-1] and workload_ports[0] <= high:
        return CheckResult(
            RENT_PORTS_CHECK,
            False,
            f'{span} overlaps the sshd port {ssh_port} or the workload ports {workload_ports[0]}-{workload_ports[-1]}',
        )
    busy = [p for p in range(low, high + 1) if not probe.port_free(p)]
    ours = probe.rental_ports()
    foreign = [p for p in busy if p not in ours]
    if foreign:
        shown = ', '.join(map(str, foreign[:8])) + (', …' if len(foreign) > 8 else '')
        return CheckResult(RENT_PORTS_CHECK, False, f'{span} in use: {shown} (pick another --rent-ports range)')
    if busy:
        return CheckResult(RENT_PORTS_CHECK, True, f'{span}: {len(busy)} held by a pod of ours (a rental in progress)')
    return CheckResult(
        RENT_PORTS_CHECK, True, f'{span} free; open it on your firewall, TCP from the internet (pods publish on it)'
    )


def check_ports(probe: HostProbe, ports: Sequence[int], report: PrereqReport) -> CheckResult:
    if report.already_up:
        return CheckResult(
            'Ports free', True, f'{", ".join(map(str, ports))} held by {AGENT_CONTAINER_NAME} (already up)'
        )
    busy = [p for p in ports if not probe.port_free(p)]
    if busy:
        return CheckResult(
            'Ports free', False, f'in use: {", ".join(map(str, busy))} (pick another --ssh-port, or free it: ss -ltnp | grep :{busy[0]})'
        )  # fmt: skip
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
    return CheckResult(name, True, f'{span} free (kept free on this box, nothing to open)')


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
        return CheckResult(name, False, f'{ip} ({source}) is not public: the controller only dials public addresses (behind a NAT, forward the sshd port and pass --ip <public address>)'), None  # fmt: skip
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
        return CheckResult(
            name,
            False,
            f'hotkey not registered on netuid {netuid} ({endpoint}): register it (btcli subnet register --netuid {netuid}) '
            'or pass the registered --wallet / --hotkey',
        )
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
    rent_ports: tuple[int, int] | None = None,
    dev_box: bool = False,
) -> PrereqReport:
    """``no_chain`` is for our own dev boxes only: no registration lookup, nothing published (so no public IP or
    reachability rows), and no hotkey needed on disk. ``public_ip`` overrides detection. ``reclaim``: a workload
    container of ours left behind will be removed, so a port it holds passes. ``agent_only`` is the box half of
    ``--publish-only``: the wallet lives on another machine, so no hotkey is needed here and nothing is looked up or
    published, but the public IP and reachability rows still run (they are what the wallet machine publishes).
    ``rent_ports`` (``--rent``) adds the Sysbox and rent-range rows; without it the box is admitted idle-only.
    ``dev_box`` (``--allow-dev-keys``, our own local builds): the Sysbox row is skipped (a Lium pod cannot run it; the
    controller then runs pods under runc with its own dev flag) and the range may be as narrow as a pod needs."""
    report = PrereqReport()
    detected = probe.vendor_detected()
    report.vendor = vendor_or_default(detected)
    report.results.append(check_vendor(detected))
    driver_rows, card_specs = check_amd_driver(probe) if report.vendor == AMD else check_driver(probe)
    report.results.extend(driver_rows)
    docker = check_docker(probe)
    report.results.append(docker)
    if report.vendor == AMD:
        report.results.append(CheckResult(TOOLKIT_CHECK, None, 'skipped (AMD box: cards attach as device nodes)'))
    else:
        report.results.append(check_toolkit(probe))
    report.results.extend(check_host(probe, card_specs))
    if docker.ok:
        report.agent_state = probe.container_state(AGENT_CONTAINER_NAME)
        report.runner_state = probe.container_state(RUNNER_CONTAINER_NAME)
        report.workloads = probe.workload_containers()
    report.results.append(check_ports(probe, [ssh_port], report))
    report.results.append(check_workload_ports(probe, workload_ports, report, reclaim))
    if rent_ports is not None:
        if dev_box:
            report.results.append(CheckResult(SYSBOX_CHECK, None, 'skipped (dev box: pods run under runc)'))
        else:
            report.results.append(check_sysbox(probe))
        minimum = RENT_PORTS_MIN_DEV if dev_box else RENT_PORTS_MIN
        report.results.append(check_rent_ports(probe, rent_ports, ssh_port, report, workload_ports, minimum))
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
