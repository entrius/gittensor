# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The GPU vendor switch (vault ``30`` §1 #1, §3): the two words the controller branches on, how a box's vendor is
detected, and the container flags that attach a card of each vendor.

The NVIDIA path is the working one and is not refactored: AMD is a sibling avenue selected by ``vendor`` at the few
points where the controller reaches for a vendor tool. The vendor is a catalog fact (``CardSpec.vendor``), detected
once at the start of every scrape and pinned on the box at admit (``BoxState.vendor``); every later decision reads
the pinned value. One vendor per box. Intel later adds a third word to the same switches.
"""

import shlex
from typing import List, Sequence

NVIDIA = 'nvidia'
AMD = 'amd'
VENDORS = (NVIDIA, AMD)
BOTH = 'both'  # both kernel modules loaded: one vendor per box, refused once the AMD avenue judges (30 §3)

# Which GPU kernel module the box runs. Read from sysfs, which the agent (privileged, host pid) sees as the host's:
# the NVIDIA driver registers ``/sys/module/nvidia``, the in-tree AMD driver ``/sys/module/amdgpu``. One line per
# module found; nothing when neither is loaded (the nvidia-smi scrape then fails closed as it does today).
# After the module lines: every AMD render node's PCI device id (``amd_device=0x74b9``) and every KFD node's
# ``simd_count`` (``amd_simd=1216``). ``amdgpu`` alone does not make a box AMD: a Ryzen's integrated display (Raphael,
# 0x164e, seen on a Lium 2x 5090 host 10/9) loads it too. The AMD side counts only when a device is a catalog AMD card
# or the KFD topology shows a compute node (simd_count > 0); a display-only AMD device beside NVIDIA cards is ignored.
VENDOR_DETECT_COMMAND = (
    'for m in nvidia amdgpu; do test -d /sys/module/$m && echo $m; done; '
    'for n in /sys/class/drm/renderD*/device; do [ "$(cat $n/vendor 2>/dev/null)" = 0x1002 ] '
    '&& echo "amd_device=$(cat $n/device 2>/dev/null)"; done; '
    'grep -h "^simd_count " /sys/class/kfd/kfd/topology/nodes/*/properties 2>/dev/null | sed "s/^simd_count /amd_simd=/"; '
    'true'
)
_MODULE_VENDOR = {'nvidia': NVIDIA, 'amdgpu': AMD}

# AMD cards are attached as device nodes, not through a container runtime (30 §1 #5): ``/dev/kfd`` (the compute
# interface, one node for every card) plus the render node of each card, never the ``card*`` primary nodes and never
# all of ``/dev/dri``. The kernel enforces the isolation: without a card's render node, KFD refuses to create a GPU VM
# on it. No ``--group-add``: a group NAME resolves inside the container, where a stock image has no ``render`` group
# (docker refuses to start it), and under Sysbox the nodes appear as nobody:nogroup, so no group bit can ever apply.
# The rule is the nodes are 0666 on the host (vault 30 §10 outcome b, measured 10/9); ``gitt up`` checks it.
AMD_KFD = '/dev/kfd'
AMD_DRI = '/dev/dri'


def parse_vendor(stdout: str) -> str:
    """``nvidia``, ``amd``, ``both`` or '' (neither module loaded). ``amdgpu`` counts as a GPU vendor only when the
    command also saw a compute-capable AMD device: a catalog AMD PCI id or a KFD node with ``simd_count`` > 0. Output
    without any ``amd_device=`` line (no AMD render node at all) keeps the module's word, as before."""
    lines = [line.strip() for line in stdout.splitlines()]
    found = {_MODULE_VENDOR[line] for line in lines if line in _MODULE_VENDOR}
    devices = [line.split('=', 1)[1].strip().lower() for line in lines if line.startswith('amd_device=')]
    simds = [line.split('=', 1)[1].strip() for line in lines if line.startswith('amd_simd=')]
    if AMD in found and devices:
        from gittensor.controller.checks.catalog import spec_for_pci_id  # noqa: PLC0415  (catalog imports this module)

        compute = any(spec_for_pci_id(d) is not None for d in devices) or any(n.isdigit() and int(n) > 0 for n in simds)
        if not compute:
            found.discard(AMD)  # an integrated display, not a card the pool could judge
    if len(found) > 1:
        return BOTH
    return found.pop() if found else ''


def vendor_or_default(detected: str) -> str:
    """The vendor the controller judges a box as. Only a box that is AMD and nothing else takes the AMD avenue;
    anything else ('' or ``both`` included) stays on the NVIDIA path, whose checks fail closed on their own."""
    return AMD if detected == AMD else NVIDIA


def render_node_path(render_node: str) -> str:
    """``/dev/dri/renderD128`` from ``renderD128``, ``128`` or an absolute path."""
    node = render_node.strip()
    if node.startswith('/'):
        return node
    if node.isdigit():
        node = f'renderD{node}'
    return f'{AMD_DRI}/{node}'


def amd_attach_args(render_nodes: Sequence[str]) -> List[str]:
    """The ``docker run`` / ``docker create`` flags that give a container the AMD cards named by their render nodes:
    one for the proof and a workload, every pinned node of the box for a rental pod (a pod takes the whole box, 29
    §1 #3, and gets exactly the nodes the scrape saw, nothing else). Already shell-quoted."""
    devices = [AMD_KFD, *(render_node_path(n) for n in render_nodes)]
    return [f'--device {shlex.quote(d)}' for d in devices]
