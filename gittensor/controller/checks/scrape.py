# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Scrape a box over the runner: the commands the controller issues and the parsers for what comes back.

Everything scraped is the miner's own number — a host-root miner can shim ``nvidia-smi`` or overlay ``/proc``
(``23`` §5) — so nothing here is a proof. It is the identity and resource picture the judges in ``checks.py`` hold
against the pinned spec; the GPU proof is the only thing the box cannot simply type.
"""

import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner

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


def disk_free_command(path: str = cfg.DISK_PATH) -> str:
    return f'df -kP {shlex.quote(path)} | tail -n 1'


def network_command(url: str, timeout_s: float = cfg.NETWORK_TIMEOUT_S) -> str:
    return f"curl -sS -o /dev/null -m {int(timeout_s)} -w '%{{http_code}} %{{speed_download}}' {shlex.quote(url)}"


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
    compute_cap: str

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


@dataclass
class HostScrape:
    gpus: List[GpuInfo] = field(default_factory=list)
    nvml_md5: str = ''
    nvml_path: str = ''
    kernel_driver: str = ''
    agent_image_digests: List[str] = field(default_factory=list)
    agent_image_id: str = ''
    disk_free_gb: Optional[float] = None
    network: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)  # scrape step -> what went wrong (fails that check)

    @property
    def driver(self) -> str:
        """The driver every card reports, or '' when cards disagree or there are none (both fail closed)."""
        drivers = {g.driver for g in self.gpus}
        return drivers.pop() if len(drivers) == 1 else ''

    @property
    def uuids(self) -> List[str]:
        return [g.uuid for g in self.gpus]


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


def scrape_host(
    runner: HostRunner,
    agent_container: str = cfg.AGENT_CONTAINER_NAME,
    disk_path: str = cfg.DISK_PATH,
    network_targets: Sequence[str] = cfg.NETWORK_TARGETS,
    timeout: float = cfg.SSH_COMMAND_TIMEOUT_S,
) -> HostScrape:
    """Every identity and resource fact the sub-checks judge, in one pass. A step that fails records its error and
    leaves its field empty; the judge for that field then fails closed."""
    scrape = HostScrape()
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
    out = _run(runner, scrape, 'agent_image', agent_image_command(agent_container), timeout)
    if out is not None:
        scrape.agent_image_digests = parse_repo_digests(out)
    out = _run(runner, scrape, 'agent_image_id', agent_image_id_command(agent_container), timeout)
    if out is not None:
        scrape.agent_image_id = parse_image_id(out)
    out = _run(runner, scrape, 'disk_free', disk_free_command(disk_path), timeout)
    if out is not None:
        scrape.disk_free_gb = parse_df_available_gb(out)
    for url in network_targets:
        out = _run(runner, scrape, f'network:{url}', network_command(url), cfg.NETWORK_TIMEOUT_S + 5)
        scrape.network[url] = parse_curl(out) if out is not None else (0, 0.0)
    return scrape
