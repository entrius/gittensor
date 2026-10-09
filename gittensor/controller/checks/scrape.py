# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Scrape a box over the runner: the commands the controller issues and the parsers for what comes back.

Everything scraped is the miner's own number — a host-root miner can shim ``nvidia-smi`` or overlay ``/proc``
(``23`` §5) — so nothing here is a proof. It is the identity and resource picture the judges in ``checks.py`` hold
against the pinned spec; the GPU proof is the only thing the box cannot simply type.
"""

import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from gittensor.agent.config import DRAIN_MARKER, RENT_PORTS_LABEL, parse_rent_ports
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.amd_scrape import AMD_SYSFS_COMMAND, AmdCard, AmdStack, parse_amd_sysfs
from gittensor.controller.checks.catalog import spec_for_pci_id
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.vendor import AMD, NVIDIA, VENDOR_DETECT_COMMAND, parse_vendor, vendor_or_default

NVIDIA_SMI_FIELDS = (
    'uuid',
    'name',
    'driver_version',
    'memory.total',
    'power.limit',
    'power.default_limit',
    'power.max_limit',
    'pci.bus_id',
    'compute_cap',
)


def nvidia_smi_command() -> str:
    return f'nvidia-smi --query-gpu={",".join(NVIDIA_SMI_FIELDS)} --format=csv,noheader,nounits'


# The NVIDIA container toolkit mounts the host's libnvidia-ml into the agent container, so this is the host's lib
# (Lium hashes the same file from inside its executor, `miner_jobs/machine_scrape.py get_libnvidia_ml_path`).
NVML_MD5_COMMAND = (
    "f=$(find /usr /lib -name 'libnvidia-ml.so.1' -print -quit 2>/dev/null); "
    '[ -n "$f" ] && md5sum "$(readlink -f "$f")"'
)
# The kernel module's own version line — a second opinion on the driver that a shimmed nvidia-smi does not control.
KERNEL_DRIVER_COMMAND = 'cat /proc/driver/nvidia/version'


def agent_image_command(container: str = cfg.AGENT_CONTAINER_NAME) -> str:
    return (
        f"docker inspect --format '{{{{.Image}}}}' {shlex.quote(container)} "
        '| xargs -r docker image inspect --format \'{{join .RepoDigests ","}}\''
    )


def agent_image_id_command(container: str = cfg.AGENT_CONTAINER_NAME) -> str:
    """The running agent container's image ID. A local build has no repo digest; on a dev box this exact ID is what
    ``FullCheckConfig.agent_image_ids`` pins instead."""
    return f"docker inspect --format '{{{{.Image}}}}' {shlex.quote(container)}"


def rent_ports_command(container: str = cfg.AGENT_CONTAINER_NAME) -> str:
    """The agent container's ``RENT_PORTS_LABEL`` (vault 29 §5): "LOW-HIGH" when the miner started it with
    ``gitt up --rent``, '' otherwise. Read on every visit: the label is on the running container, so a box is rentable
    exactly as long as its agent says so, and a controller restart rebuilds the fact from the box like everything else.
    A leaving miner's ``DRAIN_MARKER`` (`gitt down`, waiting for its customer) answers '' instead: no new pod lands
    there while they wait, and their pay is never at stake for leaving (#1818)."""
    label = f'docker inspect --format \'{{{{index .Config.Labels "{RENT_PORTS_LABEL}"}}}}\' {shlex.quote(container)}'
    return f'test -e {shlex.quote(DRAIN_MARKER)} || {label}'


def parse_rent_ports_label(stdout: str) -> List[int]:
    """``[low, high]`` for a well-formed label, else ``[]`` (no label). The range's width is `gitt up`'s policy (100 for
    a miner, 4 for a dev box): the controller records whatever range the agent carries."""
    ports = parse_rent_ports(stdout, minimum=1)
    return list(ports) if ports else []


def disk_free_command(path: str = cfg.DISK_PATH) -> str:
    """Free space on the HOST filesystem holding ``path`` ('' = the host docker daemon's root dir), seen through
    PID 1's root because the agent container's own filesystem is not the host's."""
    target = shlex.quote(path) if path else '"$(docker info --format \'{{.DockerRootDir}}\')"'
    return f'df -kP {cfg.HOST_ROOT}{target} | tail -n 1'


def network_command(url: str, timeout_s: float = cfg.NETWORK_TIMEOUT_S) -> str:
    return f"curl -sS -o /dev/null -m {int(timeout_s)} -w '%{{http_code}} %{{speed_download}}' {shlex.quote(url)}"


# The host around the cards (host specs, 10/9), vendor-neutral and read through PID 1's root like ``disk_free_command``: the
# agent runs with --pid host, so /proc/1/root is the host's filesystem and its /proc is the host's procfs.
MEMINFO_COMMAND = f'cat {cfg.HOST_ROOT}/proc/meminfo'
# `nproc --all` counts every CPU the host has (no cpuset narrows the agent); a box without coreutils' nproc is read
# from the host's cpuinfo instead.
CPU_THREADS_COMMAND = f'nproc --all 2>/dev/null || grep -c ^processor {cfg.HOST_ROOT}/proc/cpuinfo'


def download_probe_command(
    repo: str = cfg.DOWNLOAD_PROBE_REPO,
    blob: str = cfg.DOWNLOAD_PROBE_BLOB,
    timeout_s: float = cfg.DOWNLOAD_PROBE_TIMEOUT_S,
) -> str:
    """One real transfer: a pinned layer of the proof image, pulled from Docker Hub the way `docker pull` pulls it (an
    anonymous pull token, then the blob, which Hub answers with a redirect to its CDN). Prints curl's average
    bytes/s, the bytes that arrived and the final HTTP code; ``parse_download_probe`` turns a 200 with bytes on the
    wire into Mbps and anything else into no sample. A link so slow the timeout cuts the pull short (curl exit 28)
    still prints its partial count, and that is its sample: a slow box must not escape the floor by being slow.
    Exit 3 with nothing printed when no token came back (Hub down, no DNS)."""
    token_url = f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull'
    blob_url = f'https://registry-1.docker.io/v2/{repo}/blobs/{blob}'
    return (
        f'T=$(curl -sS -m 10 {shlex.quote(token_url)} | sed -n \'s/.*"token":"\\([^"]*\\)".*/\\1/p\'); '
        '[ -n "$T" ] || { echo "no pull token" >&2; exit 3; }; '
        f'curl -sS -L -m {int(timeout_s)} -o /dev/null -H "Authorization: Bearer $T" '
        f"-w '%{{speed_download}} %{{size_download}} %{{http_code}}' {shlex.quote(blob_url)} || [ $? -eq 28 ]"
    )


DOWNLOAD_PROBE_COMMAND = download_probe_command()
# The first `model name` line of the host's cpuinfo, for display only (``publish`` holds it to a pattern).
CPU_MODEL_COMMAND = f"sed -n 's/^model name[[:space:]]*: //p' {cfg.HOST_ROOT}/proc/cpuinfo | head -n 1"
# The upload probe: the box streams bytes to us over the session we hold (box -> controller); ``_timed_run`` times
# it on our clock. `tr` makes them printable so no transport or decoder can drop them.
UPLOAD_PROBE_COMMAND = f"head -c {cfg.UPLOAD_PROBE_BYTES} /dev/zero | tr '\\0' a"
# The session's own round trip (exec, nothing, exit): what one command costs before any byte moves. Subtracted from
# the upload timing, published as ``rtt_ms`` (evidence only).
RTT_COMMAND = 'true'
# The interconnect between the cards (NVIDIA): the link class of every GPU pair; the AMD sibling reads the KFD
# topology's io_links from sysfs (type 11 = XGMI, type 2 = PCIe), opening nothing on the card (30 §3).
NVIDIA_TOPO_COMMAND = 'nvidia-smi topo -m'
AMD_TOPO_COMMAND = (
    'for d in /sys/class/kfd/kfd/topology/nodes/*; do n=$(basename "$d"); '
    'r=$(grep -E \'^drm_render_minor \' "$d/properties" 2>/dev/null | cut -d" " -f2); echo "== $n render=${r:-0}"; '
    'for l in "$d"/io_links/*/properties; do [ -f "$l" ] || continue; '
    "echo \"-- $(grep -E '^(type|node_to) ' \"$l\" | tr '\\n' ' ')\"; done; done; true"
)


def topo_command(vendor: str = NVIDIA) -> str:
    return AMD_TOPO_COMMAND if vendor == AMD else NVIDIA_TOPO_COMMAND


# Every host process with an NVIDIA device node open (one `find` over the host's /proc/*/fd), then each holder's comm
# and cgroup. Exit 3 when the host procfs is not where we look; a holder that exits mid-scan prints MISSING.
DEVICE_HOLDERS_COMMAND = (
    rf'H={cfg.HOST_ROOT}/proc; [ -r "$H/1/cgroup" ] || {{ echo "no host procfs at $H" >&2; exit 3; }}; '
    r"""L=$(find "$H"/[0-9]*/fd -maxdepth 1 -lname '/dev/nvidia*' -printf '%h %l\n' 2>/dev/null); printf '%s\n' "$L"; """
    r"""for p in $(printf '%s\n' "$L" | sed -n 's#^.*/proc/\([0-9]*\)/fd .*#\1#p' | sort -un); do """
    r"""printf '== %s %s\n' "$p" "$(cat "$H/$p/comm" 2>/dev/null)"; cat "$H/$p/cgroup" 2>/dev/null || echo MISSING; """
    r'done; exit 0'
)
# The same scan on an AMD box: the compute interface and the render nodes (30 §1 #5). A process holding only a
# `card*` primary node (a display server's modesetting handle) is not a GPU holder here; the compute path is KFD.
AMD_DEVICE_HOLDERS_COMMAND = DEVICE_HOLDERS_COMMAND.replace(
    "-lname '/dev/nvidia*'", "\\( -lname /dev/kfd -o -lname '/dev/dri/renderD*' \\)"
)
CONTAINER_ID = re.compile(r'[0-9a-f]{64}')
_HOLDER_FD = re.compile(r'/proc/(\d+)/fd (/dev/\S+)$')
_GPU_DEVICE = re.compile(r'^/dev/nvidia(\d+|ctl|-uvm)$')  # the nodes a CUDA or `--gpus` process holds
AMD_GPU_DEVICE = re.compile(r'^/dev/(kfd|dri/renderD\d+)$')  # the nodes a HIP process or an AMD pod holds


def device_holders_command(vendor: str = NVIDIA) -> str:
    return AMD_DEVICE_HOLDERS_COMMAND if vendor == AMD else DEVICE_HOLDERS_COMMAND


def gpu_device_pattern(vendor: str = NVIDIA) -> 're.Pattern[str]':
    return AMD_GPU_DEVICE if vendor == AMD else _GPU_DEVICE


PERSISTENCED_COMM = 'nvidia-persiste'  # /proc/<pid>/comm stops at 15 bytes: nvidia-persistenced


@dataclass
class GpuInfo:
    uuid: str
    name: str
    driver: str
    memory_total_mib: Optional[int]
    power_limit_w: Optional[float]
    power_default_limit_w: Optional[float]
    power_max_limit_w: Optional[float]
    pci_bus_id: str
    compute_cap: str  # NVIDIA: the compute capability; AMD: the gfx target (30 §3)
    vendor: str = NVIDIA
    render_node: str = ''  # AMD only: the card's /dev/dri/renderD<N>, what a container is given to see it

    @property
    def memory_total_bytes(self) -> Optional[int]:
        return None if self.memory_total_mib is None else self.memory_total_mib * 1024 * 1024

    def as_dict(self) -> dict:
        return dict(vars(self))


def _num(value: str, cast):
    value = value.strip()
    if not value or value.startswith('[') or value.upper() in ('N/A', 'NA', 'NONE'):
        return None
    try:
        return cast(float(value))
    except ValueError:
        return None


def parse_nvidia_smi(stdout: str) -> List[GpuInfo]:
    """One ``GpuInfo`` per non-blank line of ``--format=csv,noheader,nounits``. ``[N/A]`` fields become None; a line
    with the wrong column count raises ``ValueError`` (a shimmed or truncated nvidia-smi)."""
    gpus: List[GpuInfo] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        cols = [c.strip() for c in line.split(',')]
        if len(cols) != len(NVIDIA_SMI_FIELDS):
            raise ValueError(f'nvidia-smi line has {len(cols)} columns, expected {len(NVIDIA_SMI_FIELDS)}: {line!r}')
        gpus.append(
            GpuInfo(
                uuid=cols[0],
                name=cols[1],
                driver=cols[2],
                memory_total_mib=_num(cols[3], int),
                power_limit_w=_num(cols[4], float),
                power_default_limit_w=_num(cols[5], float),
                power_max_limit_w=_num(cols[6], float),
                pci_bus_id=cols[7],
                compute_cap=cols[8],
            )
        )
    return gpus


_MD5 = re.compile(r'\b([0-9a-fA-F]{32})\b')
_KERNEL_DRIVER = re.compile(r'NVRM version:.*?\s(\d+\.\d+(?:\.\d+)*)(?:\s|$)')


def parse_md5(stdout: str) -> str:
    m = _MD5.search(stdout)
    return m.group(1).lower() if m else ''


def parse_kernel_driver(stdout: str) -> str:
    m = _KERNEL_DRIVER.search(stdout)
    return m.group(1) if m else ''


def parse_repo_digests(stdout: str) -> List[str]:
    """``repo@sha256:...`` entries (comma-joined) to their ``sha256:...`` digests."""
    digests = []
    for token in stdout.replace('\n', ',').split(','):
        token = token.strip()
        if not token:
            continue
        digests.append(token.rsplit('@', 1)[-1] if '@' in token else token)
    return digests


_IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')


def parse_image_id(stdout: str) -> str:
    """``sha256:<64 hex>`` or '' for anything else."""
    value = stdout.strip()
    return value if _IMAGE_ID.match(value) else ''


def parse_df_available_gb(stdout: str) -> Optional[float]:
    """Available KiB (4th column of ``df -kP``) in GB (1e9)."""
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ''
    cols = line.split()
    if len(cols) < 4:
        return None
    try:
        return int(cols[3]) * 1024 / 1e9
    except ValueError:
        return None


def parse_curl(stdout: str) -> Tuple[int, float]:
    """``(http_code, bytes/s)`` from ``-w '%{http_code} %{speed_download}'``; ``(0, 0.0)`` when curl printed nothing."""
    parts = stdout.strip().split()
    try:
        code = int(parts[0]) if parts else 0
        speed = float(parts[1]) if len(parts) > 1 else 0.0
    except ValueError:
        return 0, 0.0
    return code, speed


def parse_df_total_gb(stdout: str) -> Optional[float]:
    """Size KiB (2nd column of ``df -kP``, the same line ``parse_df_available_gb`` reads) in GB (1e9)."""
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ''
    cols = line.split()
    if len(cols) < 4:
        return None
    try:
        return int(cols[1]) * 1024 / 1e9
    except ValueError:
        return None


_MEMTOTAL = re.compile(r'^MemTotal:\s+(\d+)\s*kB', re.M)


def parse_meminfo_total_gb(stdout: str) -> Optional[float]:
    """``MemTotal`` of /proc/meminfo in GB (1e9); None when the line is missing."""
    m = _MEMTOTAL.search(stdout)
    return int(m.group(1)) * 1024 / 1e9 if m else None


def parse_cpu_threads(stdout: str) -> Optional[int]:
    """``nproc --all`` (or a cpuinfo line count): a positive integer, else None."""
    value = stdout.strip().split()[-1] if stdout.strip() else ''
    try:
        n = int(value)
    except ValueError:
        return None
    return n if n > 0 else None


@dataclass
class DownloadProbe:
    """What one run of ``DOWNLOAD_PROBE_COMMAND`` printed. ``mbps`` is a sample only for a complete 200 with bytes
    on the wire; anything else (a miss, a redirect that went nowhere, an empty body) is ``None``: no sample, never a
    failure, so a Hub hiccup can never bench a box (9/19)."""

    http_code: int = 0
    bytes: int = 0
    bytes_per_s: float = 0.0

    @property
    def mbps(self) -> Optional[float]:
        if self.http_code != 200 or self.bytes <= 0 or self.bytes_per_s <= 0:
            return None
        return self.bytes_per_s * 8 / 1e6

    def as_dict(self) -> dict:
        return {'http_code': self.http_code, 'bytes': self.bytes, 'bytes_per_s': self.bytes_per_s, 'mbps': self.mbps}


# Interconnect classes, published as one word: a bonded NVLink set (NVIDIA) or XGMI (AMD) is the fast class, anything
# through PCIe (a switch, the host bridge, or across sockets) is ``pcie``; a one-card box has no link to classify.
NVLINK, XGMI, PCIE, SINGLE = 'nvlink', 'xgmi', 'pcie', 'single'
# nvidia-smi's link classes, best first; the worst pair on the box is what the customer is promised.
_NVIDIA_LINK_ORDER = ('NV', 'PIX', 'PXB', 'PHB', 'NODE', 'SYS')


@dataclass
class Interconnect:
    cls: str = ''  # NVLINK / XGMI / PCIE / SINGLE; '' when it could not be read
    raw: str = ''  # the worst link as the tool names it: 'NV18', 'PHB', 'XGMI', 'PCIE'

    def as_dict(self) -> dict:
        return {'interconnect': self.cls or None, 'interconnect_raw': self.raw or None}


def _link_rank(raw: str) -> int:
    for i, prefix in enumerate(_NVIDIA_LINK_ORDER):
        if raw.startswith(prefix):
            return i
    return len(_NVIDIA_LINK_ORDER)  # a class we do not know is treated as the worst


def parse_nvidia_topo(stdout: str) -> Interconnect:
    """``nvidia-smi topo -m``: the matrix header names the columns (GPUs, then NICs and affinities, which are
    skipped), one row per GPU. The worst link between any two GPUs decides; one GPU is ``single``."""
    lines = [line.rstrip() for line in stdout.splitlines()]
    header = next((line for line in lines if line.lstrip().startswith('GPU0')), None)
    if header is None:
        return Interconnect()
    columns = header.split()
    gpu_cols = [i for i, name in enumerate(columns) if re.fullmatch(r'GPU\d+', name)]
    worst: Optional[str] = None
    rows = 0
    for line in lines:
        cells = line.split()
        if line is header or not cells or not re.fullmatch(r'GPU\d+', cells[0]):
            continue
        rows += 1
        for i in gpu_cols:
            if i + 1 >= len(cells) or columns[i] == cells[0]:
                continue
            raw = cells[i + 1]
            if raw == 'X':
                continue
            if worst is None or _link_rank(raw) > _link_rank(worst):
                worst = raw
    if rows == 0:
        return Interconnect()
    if rows == 1 or worst is None:
        return Interconnect(SINGLE, 'X')
    return Interconnect(NVLINK if worst.startswith('NV') else PCIE, worst)


_KFD_XGMI, _KFD_PCIE = 11, 2  # CRAT io_link types


def parse_amd_topo(stdout: str) -> Interconnect:
    """``AMD_TOPO_COMMAND``: the KFD nodes (a GPU has a render minor) and each node's io_links. Every GPU pair joined
    by an XGMI link is the fast class; any pair without one goes through PCIe."""
    gpus: set[str] = set()
    links: Dict[str, Dict[str, int]] = {}
    node = ''
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith('== '):
            node, _, render = line[3:].partition(' ')
            minor = render.partition('=')[2].strip()
            if minor.isdigit() and int(minor) > 0:
                gpus.add(node)
        elif line.startswith('-- ') and node:
            m = re.search(r'type (\d+) node_to (\d+)', line)
            if m:
                links.setdefault(node, {})[m.group(2)] = int(m.group(1))
    if not gpus:
        return Interconnect()
    if len(gpus) == 1:
        return Interconnect(SINGLE, 'X')
    for a in gpus:
        for b in gpus:
            if a != b and links.get(a, {}).get(b) != _KFD_XGMI:
                return Interconnect(PCIE, 'PCIE')
    return Interconnect(XGMI, 'XGMI')


def parse_cpu_model(stdout: str) -> str:
    """The model name, whitespace collapsed, at most 64 characters; '' when there is none."""
    line = stdout.strip().splitlines()[0] if stdout.strip() else ''
    return ' '.join(line.split())[:64]


@dataclass
class UploadProbe:
    """One run of ``UPLOAD_PROBE_COMMAND`` on our clock: the bytes that arrived and the seconds they took, less the
    session's own round trip. A short read or no time is no sample."""

    bytes: int = 0
    seconds: float = 0.0
    rtt_s: Optional[float] = None

    @property
    def mbps(self) -> Optional[float]:
        if self.bytes < cfg.UPLOAD_PROBE_BYTES:
            return None
        wire = self.seconds - (self.rtt_s or 0.0)
        return self.bytes * 8 / 1e6 / wire if wire > 0 else None

    def as_dict(self) -> dict:
        return {'bytes': self.bytes, 'seconds': round(self.seconds, 4), 'rtt_s': self.rtt_s, 'mbps': self.mbps}


def parse_download_probe(stdout: str) -> DownloadProbe:
    """``-w '%{speed_download} %{size_download} %{http_code}'`` to a ``DownloadProbe``; a line that does not parse is
    a probe with nothing in it."""
    parts = stdout.strip().split()
    try:
        speed = float(parts[0]) if parts else 0.0
        size = int(float(parts[1])) if len(parts) > 1 else 0
        code = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return DownloadProbe()
    return DownloadProbe(code, size, speed)


@dataclass
class DeviceHolder:
    pid: int
    devices: list[str] = field(default_factory=list)
    comm: str = ''
    read: bool = False  # its comm + cgroup block came back
    containers: set[str] | None = field(default_factory=set)  # IDs in its cgroup paths; None: exited mid-scan


def parse_device_holders(stdout: str, device: 're.Pattern[str]' = _GPU_DEVICE) -> dict[int, DeviceHolder]:
    """``DEVICE_HOLDERS_COMMAND``'s output: the fd lines (only the GPU nodes we judge, ``device``: NVIDIA's by
    default, ``AMD_GPU_DEVICE`` on an AMD box), then a block per holder."""
    holders: dict[int, DeviceHolder] = {}
    current: DeviceHolder | None = None
    in_blocks = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith('== '):
            in_blocks = True
            pid_text, _, comm = line[3:].partition(' ')
            current = holders.get(int(pid_text)) if pid_text.isdigit() else None
            if current is not None:
                current.comm, current.read, current.containers = comm.strip(), True, set()
        elif not in_blocks:
            m = _HOLDER_FD.search(line)
            if m and device.match(m.group(2)):
                holder = holders.setdefault(int(m.group(1)), DeviceHolder(int(m.group(1))))
                if m.group(2) not in holder.devices:
                    holder.devices.append(m.group(2))
        elif current is not None:
            if line == 'MISSING':
                current.containers = None
            elif current.containers is not None:
                current.containers.update(CONTAINER_ID.findall(line))
    return holders


@dataclass
class HostScrape:
    # As detected (``vendor.parse_vendor``: 'nvidia', 'amd', 'both' or ''); ``vendor`` is what the box is judged as.
    vendor_detected: str = ''
    gpus: List[GpuInfo] = field(default_factory=list)
    nvml_md5: str = ''
    nvml_path: str = ''
    kernel_driver: str = ''
    agent_image_digests: List[str] = field(default_factory=list)
    agent_image_id: str = ''
    rent_ports: List[int] = field(default_factory=list)  # [low, high] from the agent's label; [] = not for rent
    disk_free_gb: Optional[float] = None
    # The host around the cards (host specs, 10/9): None where the step failed (``errors`` names it) or, for the download,
    # where the probe did not run this visit or got no sample (``down_probe`` says which).
    ram_total_gb: Optional[float] = None
    cpu_threads: Optional[int] = None
    cpu_model: str = ''
    disk_total_gb: Optional[float] = None
    down_mbps: Optional[float] = None
    down_probe: Optional[DownloadProbe] = None  # None: the probe was not run this visit
    up_mbps: Optional[float] = None
    up_probe: Optional[UploadProbe] = None  # None: the probe was not run this visit
    rtt_ms: Optional[float] = None  # the session's round trip on our clock (evidence only)
    interconnect: Optional[Interconnect] = None  # None: the topology step failed
    network: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    device_holders: str = ''  # DEVICE_HOLDERS_COMMAND's raw stdout; ``checks.check_card_free`` parses and judges it
    errors: Dict[str, str] = field(default_factory=dict)  # scrape step -> what went wrong (fails that check)
    # The AMD avenue (30 §3): the cards as sysfs shows them (``gpus`` is built from these) and the stack record.
    amd_cards: List[AmdCard] = field(default_factory=list)
    amd_stack: Optional[AmdStack] = None

    @property
    def vendor(self) -> str:
        return vendor_or_default(self.vendor_detected)

    @property
    def driver(self) -> str:
        """The driver every card reports, or '' when cards disagree or there are none (both fail closed)."""
        drivers = {g.driver for g in self.gpus}
        return drivers.pop() if len(drivers) == 1 else ''

    @property
    def uuids(self) -> List[str]:
        return [g.uuid for g in self.gpus]


def _timed_run(
    runner: HostRunner, scrape: HostScrape, step: str, command: str, timeout: float, clock: Callable[[], float]
) -> Tuple[Optional[str], float]:
    """``_run`` with the seconds the command took on our clock (what the bandwidth probes are judged by)."""
    t0 = clock()
    out = _run(runner, scrape, step, command, timeout)
    return out, max(0.0, clock() - t0)


def _run(runner: HostRunner, scrape: HostScrape, step: str, command: str, timeout: float) -> Optional[str]:
    try:
        result = runner.run(command, timeout=timeout)
    except Exception as e:  # transport died mid-scrape; the step fails, the rest still run
        scrape.errors[step] = f'{type(e).__name__}: {e}'[:300]
        return None
    if not result.ok:
        scrape.errors[step] = f'exit {result.exit_code}: {(result.stderr or result.stdout).strip()[:300]}'
        return None
    return result.stdout


def _scrape_nvidia(runner: HostRunner, scrape: HostScrape, timeout: float) -> None:
    """The NVIDIA identity steps, as they have always run."""
    out = _run(runner, scrape, 'nvidia_smi', nvidia_smi_command(), cfg.NVIDIA_SMI_TIMEOUT_S)
    if out is not None:
        try:
            scrape.gpus = parse_nvidia_smi(out)
        except ValueError as e:
            scrape.errors['nvidia_smi'] = str(e)[:300]
    out = _run(runner, scrape, 'nvml_md5', NVML_MD5_COMMAND, timeout)
    if out is not None:
        scrape.nvml_md5 = parse_md5(out)
        scrape.nvml_path = out.strip().split(None, 1)[1] if len(out.split()) > 1 else ''
    out = _run(runner, scrape, 'kernel_driver', KERNEL_DRIVER_COMMAND, timeout)
    if out is not None:
        scrape.kernel_driver = parse_kernel_driver(out)


def _scrape_amd(runner: HostRunner, scrape: HostScrape, timeout: float) -> None:
    """The AMD sibling (30 §3): one sysfs pass gives the cards, their render nodes and the stack; nothing on this
    path opens ``/dev/kfd``. Each card becomes a ``GpuInfo`` the vendor-neutral checks (uuid pin, fleet uniqueness,
    power) read as they read an NVIDIA card's; ``check_amd_spec`` judges the AMD-only fields."""
    out = _run(runner, scrape, 'amd_sysfs', AMD_SYSFS_COMMAND, timeout)
    if out is None:
        return
    scrape.amd_cards, scrape.amd_stack = parse_amd_sysfs(out)
    scrape.gpus = [amd_gpu_info(c, scrape.amd_stack) for c in scrape.amd_cards]


def amd_gpu_info(card: AmdCard, stack: AmdStack) -> GpuInfo:
    """An AMD card in the shape every vendor-neutral check reads. The name is the catalog's display name for the
    card's PCI id (an AMD card is matched on ids, never on a marketing string; sysfs's ``product_name`` is recorded
    only), the driver is the kernel release (the in-tree driver has no version of its own), the compute capability
    is the gfx target, the power fields are hwmon's cap in watts."""
    spec = spec_for_pci_id(card.device_id)
    name = spec.name if spec is not None else (card.product_name or f'AMD {card.device_id or "?"}')
    return GpuInfo(
        uuid=card.uuid,
        name=name,
        driver=stack.kernel,
        memory_total_mib=card.memory_total_mib,
        power_limit_w=card.power_cap_w,
        power_default_limit_w=card.power_cap_default_w,
        power_max_limit_w=card.power_cap_max_w,
        pci_bus_id=card.pci,
        compute_cap=card.gfx_target,
        vendor=AMD,
        render_node=card.render_node,
    )


def scrape_host(
    runner: HostRunner,
    agent_container: str = cfg.AGENT_CONTAINER_NAME,
    disk_path: str = cfg.DISK_PATH,
    network_targets: Sequence[str] = cfg.NETWORK_TARGETS,
    timeout: float = cfg.SSH_COMMAND_TIMEOUT_S,
    download: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> HostScrape:
    """Every identity and resource fact the sub-checks judge, in one pass. A step that fails records its error and
    leaves its field empty; the judge for that field then fails closed. ``download``: run the bandwidth probes (one
    real transfer each way, ``DOWNLOAD_PROBE_COMMAND`` and ``UPLOAD_PROBE_COMMAND``); the caller caps them at one
    per box per round. ``clock`` times the upload and the round trip."""
    scrape = HostScrape()
    out = _run(runner, scrape, 'vendor', VENDOR_DETECT_COMMAND, timeout)
    if out is not None:
        scrape.vendor_detected = parse_vendor(out)
    # The vendor switch (30 §3): the NVIDIA steps unchanged, the AMD sibling beside them.
    if scrape.vendor == AMD:
        _scrape_amd(runner, scrape, timeout)
    else:
        _scrape_nvidia(runner, scrape, timeout)
    out = _run(runner, scrape, 'agent_image', agent_image_command(agent_container), timeout)
    if out is not None:
        scrape.agent_image_digests = parse_repo_digests(out)
    out = _run(runner, scrape, 'agent_image_id', agent_image_id_command(agent_container), timeout)
    if out is not None:
        scrape.agent_image_id = parse_image_id(out)
    out = _run(runner, scrape, 'rent_ports', rent_ports_command(agent_container), timeout)
    if out is not None:
        scrape.rent_ports = parse_rent_ports_label(out)
    out = _run(runner, scrape, 'disk_free', disk_free_command(disk_path), timeout)
    if out is not None:
        scrape.disk_free_gb = parse_df_available_gb(out)
        scrape.disk_total_gb = parse_df_total_gb(out)  # the same df call, its size column
    out = _run(runner, scrape, 'meminfo', MEMINFO_COMMAND, timeout)
    if out is not None:
        scrape.ram_total_gb = parse_meminfo_total_gb(out)
    out = _run(runner, scrape, 'cpu_threads', CPU_THREADS_COMMAND, timeout)
    if out is not None:
        scrape.cpu_threads = parse_cpu_threads(out)
    out = _run(runner, scrape, 'cpu_model', CPU_MODEL_COMMAND, timeout)
    if out is not None:
        scrape.cpu_model = parse_cpu_model(out)
    out = _run(runner, scrape, 'topo', topo_command(scrape.vendor), timeout)
    if out is not None:
        scrape.interconnect = parse_amd_topo(out) if scrape.vendor == AMD else parse_nvidia_topo(out)
    out, rtt_s = _timed_run(runner, scrape, 'rtt', RTT_COMMAND, timeout, clock)
    if out is not None:
        scrape.rtt_ms = round(rtt_s * 1000.0, 1)
    if download:
        out = _run(runner, scrape, 'download', DOWNLOAD_PROBE_COMMAND, cfg.DOWNLOAD_PROBE_TIMEOUT_S + 15)
        scrape.down_probe = parse_download_probe(out) if out is not None else DownloadProbe()
        scrape.down_mbps = scrape.down_probe.mbps
        out, seconds = _timed_run(runner, scrape, 'upload', UPLOAD_PROBE_COMMAND, cfg.UPLOAD_PROBE_TIMEOUT_S, clock)
        rtt = rtt_s if scrape.rtt_ms is not None else None
        scrape.up_probe = UploadProbe(len(out), seconds, rtt) if out is not None else UploadProbe()
        scrape.up_mbps = scrape.up_probe.mbps
    out = _run(runner, scrape, 'device_holders', device_holders_command(scrape.vendor), timeout)
    if out is not None:
        scrape.device_holders = out
    for url in network_targets:
        out = _run(runner, scrape, f'network:{url}', network_command(url), cfg.NETWORK_TIMEOUT_S + 5)
        scrape.network[url] = parse_curl(out) if out is not None else (0, 0.0)
    return scrape
