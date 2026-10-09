# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The AMD box scrape (vault 30 §3, 31 step 3): what the controller reads off an AMD box and the parsers for it.

The NVIDIA scrape asks ``nvidia-smi``. An AMD box has no equivalent the pool should depend on (``rocm-smi`` needs the
ROCm userland on the host, which the plan does not require), so the picture comes from sysfs, which the in-tree
``amdgpu`` driver publishes for every card: the SMU serial (``unique_id``), the PCI ids, the VRAM size, the partition
mode, the VBIOS, and the power cap through hwmon. The KFD topology joins each card to its render node and names its
gfx target. One command reads all of it; nothing here opens ``/dev/kfd`` or a render node (a GPU reset kills every
holder of ``/dev/kfd``, and the agent must never be one; a test holds the commands to that).

Everything scraped is the miner's own number, as on NVIDIA: identity on AMD is a soft check and the sealed proof's
fill floor carries the trust (``30`` §14). The ``AMD-<16 hex>`` id is what the proof's key is derived from, beside
the gfx target (``31`` step 2).
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# One pass over every amdgpu card (the DRM device behind each render node whose vendor is AMD's 0x1002), then every
# KFD topology node, then the stack. A missing attribute prints an empty value, never an error: a consumer card has
# no partition files, an in-tree driver has no /sys/module/amdgpu/version (the kernel release stands in for it).
AMD_VENDOR_ID = '0x1002'  # the PCI vendor id every amdgpu card's sysfs `vendor` file prints
AMD_SYSFS_COMMAND = (
    'for n in /sys/class/drm/renderD*; do d="$n/device"; '
    f'[ "$(cat "$d/vendor" 2>/dev/null)" = {AMD_VENDOR_ID} ] || continue; '
    'echo "== $(basename "$n") $(basename "$(readlink -f "$d")")"; '
    'for f in unique_id vendor device product_name mem_info_vram_total current_compute_partition '
    'current_memory_partition vbios_version; do printf \'%s=%s\\n\' "$f" "$(cat "$d/$f" 2>/dev/null)"; done; '
    'for h in "$d"/hwmon/hwmon*; do [ -d "$h" ] || continue; for f in power1_cap power1_cap_default power1_cap_max; '
    'do printf \'%s=%s\\n\' "$f" "$(cat "$h/$f" 2>/dev/null)"; done; break; done; done; '
    'for p in /sys/class/kfd/kfd/topology/nodes/*/properties; do [ -f "$p" ] || continue; '
    'echo "-- $(basename "$(dirname "$p")")"; '
    'grep -E \'^(unique_id|drm_render_minor|gfx_target_version) \' "$p"; done; '
    'echo "kernel=$(uname -r)"; echo "amdgpu=$(cat /sys/module/amdgpu/version 2>/dev/null)"; '
    # the device nodes' owning groups, as NUMBERS: docker resolves --group-add names inside the container, and a
    # stock image has no `render` group (the 10/9 droplet: "Unable to find group render"); render's gid is dynamic
    'for g in video render; do echo "gid_$g=$(getent group $g | cut -d: -f3)"; done; true'
)

_UNIQUE_ID = re.compile(r'^[0-9a-f]{16}$')
_CARD_HEADER = re.compile(r'^== (renderD\d+) (\S+)$')
_NODE_HEADER = re.compile(r'^-- (\d+)$')
_VERSION = re.compile(r'(\d+)\.(\d+)')
# Whole cards only (30 §14 #3): the single-partition modes of an MI300-class card. A card that reports no mode (a
# consumer card) is whole by construction.
WHOLE_COMPUTE_PARTITIONS = ('', 'SPX')
WHOLE_MEMORY_PARTITIONS = ('', 'NPS1')


@dataclass
class AmdCard:
    """One amdgpu card as sysfs shows it, joined to its KFD node."""

    render_node: str  # 'renderD128'
    pci: str  # '0000:03:00.0'
    unique_id: str = ''  # 16 lowercase hex, the SMU serial; '' when the driver publishes none
    device_id: str = ''  # '0x74a1'
    product_name: str = ''
    vram_bytes: Optional[int] = None
    compute_partition: str = ''  # 'SPX', 'CPX', ... or '' (no partition modes on this card)
    memory_partition: str = ''  # 'NPS1', 'NPS4', ... or ''
    vbios: str = ''
    power_cap_w: Optional[float] = None
    power_cap_default_w: Optional[float] = None
    power_cap_max_w: Optional[float] = None
    gfx_target: str = ''  # from the KFD node joined on the render minor, e.g. 'gfx942'
    kfd_unique_id: str = ''  # the KFD node's id as hex, for the cross-check against the DRM one

    @property
    def uuid(self) -> str:
        """The pool's spelling of the card id: ``AMD-<16 hex>``, disjoint from NVIDIA's ``GPU-`` space."""
        return f'AMD-{self.unique_id}'

    @property
    def id_ok(self) -> bool:
        """A serial the driver published, non-zero, and the same one KFD shows for the node."""
        if not _UNIQUE_ID.match(self.unique_id) or set(self.unique_id) == {'0'}:
            return False
        return not self.kfd_unique_id or self.kfd_unique_id == self.unique_id

    @property
    def whole(self) -> bool:
        return self.compute_partition in WHOLE_COMPUTE_PARTITIONS and self.memory_partition in WHOLE_MEMORY_PARTITIONS

    @property
    def partition(self) -> str:
        return f'{self.compute_partition or "-"}/{self.memory_partition or "-"}'

    @property
    def memory_total_mib(self) -> Optional[int]:
        return None if self.vram_bytes is None else self.vram_bytes // (1024 * 1024)


@dataclass
class AmdStack:
    """The driver stack record (30 §14 #2): recorded on every box, with one floor and no allowlist."""

    kernel: str = ''  # uname -r
    amdgpu: str = ''  # /sys/module/amdgpu/version: the DKMS driver's version; '' for the in-tree driver
    gids: Tuple[int, ...] = ()  # the host's video and render gids (numeric: what --group-add must say, 30 §1 #5)

    def as_dict(self) -> dict:
        return {'kernel': self.kernel, 'amdgpu': self.amdgpu, 'gids': list(self.gids)}


def version_tuple(text: str) -> Tuple[int, int]:
    """``(major, minor)`` of the first ``N.M`` in ``text``; ``(0, 0)`` when there is none."""
    m = _VERSION.search(text or '')
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def decode_gfx_target(version: int) -> str:
    """KFD's ``gfx_target_version`` (major * 10000 + minor * 100 + stepping) as the target string the compiler and
    the catalog use: 90402 → ``gfx942``, 90010 → ``gfx90a``, 120001 → ``gfx1201``. 0 (a CPU node) → ''."""
    if version <= 0:
        return ''
    major, minor, step = version // 10000, (version // 100) % 100, version % 100
    return f'gfx{major}{minor:x}{step:x}'


def _microwatts(value: str) -> Optional[float]:
    try:
        return int(value.strip()) / 1_000_000.0 if value.strip() else None
    except ValueError:
        return None


def _int(value: str) -> Optional[int]:
    try:
        return int(value.strip()) if value.strip() else None
    except ValueError:
        return None


def parse_amd_sysfs(stdout: str) -> Tuple[List[AmdCard], AmdStack]:
    """``AMD_SYSFS_COMMAND``'s output: the cards in render-node order, joined to their KFD nodes, and the stack."""
    cards: List[AmdCard] = []
    nodes: List[dict] = []
    stack = AmdStack()
    card: Optional[AmdCard] = None
    node: Optional[dict] = None
    for raw in stdout.splitlines():
        line = raw.rstrip('\n')
        header = _CARD_HEADER.match(line)
        if header:
            card, node = AmdCard(header.group(1), header.group(2)), None
            cards.append(card)
            continue
        header = _NODE_HEADER.match(line)
        if header:
            node, card = {'node': int(header.group(1))}, None
            nodes.append(node)
            continue
        if line.startswith('kernel='):
            stack.kernel, card, node = line[len('kernel=') :].strip(), None, None
        elif line.startswith('amdgpu='):
            stack.amdgpu, card, node = line[len('amdgpu=') :].strip(), None, None
        elif line.startswith('gid_'):
            value = line.split('=', 1)[1].strip()
            if value.isdigit():
                stack.gids, card, node = (*stack.gids, int(value)), None, None
        elif card is not None and '=' in line:
            key, _, value = line.partition('=')
            _set_card(card, key.strip(), value.strip())
        elif node is not None and ' ' in line:
            key, _, value = line.partition(' ')
            node[key.strip()] = value.strip()
    by_minor = {}
    for n in nodes:
        minor = _int(n.get('drm_render_minor', ''))
        if minor:  # 0 is a CPU node
            by_minor[minor] = n
    for c in cards:
        n = by_minor.get(_int(c.render_node[len('renderD') :]) or -1)
        if n is None:
            continue
        c.gfx_target = decode_gfx_target(_int(n.get('gfx_target_version', '')) or 0)
        kfd_id = _int(n.get('unique_id', ''))  # KFD prints the serial in decimal, DRM in hex
        c.kfd_unique_id = f'{kfd_id:016x}' if kfd_id else ''
    return cards, stack


def _set_card(card: AmdCard, key: str, value: str) -> None:
    if key == 'unique_id':
        card.unique_id = value.lower()
    elif key == 'device':
        card.device_id = value.lower()
    elif key == 'product_name':
        card.product_name = value
    elif key == 'mem_info_vram_total':
        card.vram_bytes = _int(value)
    elif key == 'current_compute_partition':
        card.compute_partition = value.upper()
    elif key == 'current_memory_partition':
        card.memory_partition = value.upper()
    elif key == 'vbios_version':
        card.vbios = value
    elif key == 'power1_cap':
        card.power_cap_w = _microwatts(value)
    elif key == 'power1_cap_default':
        card.power_cap_default_w = _microwatts(value)
    elif key == 'power1_cap_max':
        card.power_cap_max_w = _microwatts(value)
