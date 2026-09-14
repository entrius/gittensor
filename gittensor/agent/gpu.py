# The MIT License (MIT)
# Copyright © 2025 Entrius

"""GPU inventory for ``GET /info``: UUIDs, names, driver — NVML through ctypes, ``nvidia-smi`` as the fallback.

Telemetry only. A host can say anything here; the controller's full check (WS-C) is what it trusts. No pynvml in
the dependency tree, so the NVML calls are made directly against ``libnvidia-ml.so.1`` (the container toolkit
mounts it into any ``--gpus`` container).
"""

from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

NVML_LIBRARY = 'libnvidia-ml.so.1'
NVML_SUCCESS = 0
_NVML_BUF = 96


@dataclass(frozen=True)
class GpuInfo:
    uuid: str
    name: str
    memory_total_mib: int | None = None


@dataclass
class GpuInventory:
    driver_version: str | None
    gpus: list[GpuInfo] = field(default_factory=list)
    source: str = 'none'  # nvml | nvidia-smi | none
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            'driver_version': self.driver_version,
            'gpus': [asdict(g) for g in self.gpus],
            'source': self.source,
            'error': self.error,
        }


class _NvmlMemory(ctypes.Structure):
    _fields_ = [('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong), ('used', ctypes.c_ulonglong)]


def inventory_via_nvml(library: str = NVML_LIBRARY) -> GpuInventory:
    """Enumerate devices with NVML. Any failure is reported in ``error``; nothing raises."""
    try:
        nvml = ctypes.CDLL(library)
    except OSError as e:
        return GpuInventory(None, source='nvml', error=f'{library}: {e}')
    rc = nvml.nvmlInit_v2()
    if rc != NVML_SUCCESS:
        return GpuInventory(None, source='nvml', error=f'nvmlInit_v2 rc={rc}')
    try:
        buf = ctypes.create_string_buffer(_NVML_BUF)
        driver = None
        if nvml.nvmlSystemGetDriverVersion(buf, ctypes.c_uint(_NVML_BUF)) == NVML_SUCCESS:
            driver = buf.value.decode(errors='replace') or None
        count = ctypes.c_uint(0)
        rc = nvml.nvmlDeviceGetCount_v2(ctypes.byref(count))
        if rc != NVML_SUCCESS:
            return GpuInventory(driver, source='nvml', error=f'nvmlDeviceGetCount_v2 rc={rc}')
        gpus = []
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if nvml.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(index), ctypes.byref(handle)) != NVML_SUCCESS:
                continue
            uuid = name = ''
            if nvml.nvmlDeviceGetUUID(handle, buf, ctypes.c_uint(_NVML_BUF)) == NVML_SUCCESS:
                uuid = buf.value.decode(errors='replace')
            if nvml.nvmlDeviceGetName(handle, buf, ctypes.c_uint(_NVML_BUF)) == NVML_SUCCESS:
                name = buf.value.decode(errors='replace')
            mem = _NvmlMemory()
            total_mib = None
            if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == NVML_SUCCESS:
                total_mib = int(mem.total // (1024 * 1024))
            gpus.append(GpuInfo(uuid=uuid, name=name, memory_total_mib=total_mib))
        return GpuInventory(driver, gpus, source='nvml')
    finally:
        nvml.nvmlShutdown()


def parse_nvidia_smi_csv(text: str) -> tuple[str | None, list[GpuInfo]]:
    """Parse ``--query-gpu=uuid,name,driver_version,memory.total --format=csv,noheader,nounits`` output."""
    driver = None
    gpus = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split(',')]
        if len(cols) < 3 or not cols[0]:
            continue
        driver = driver or cols[2] or None
        total = None
        if len(cols) >= 4 and cols[3].isdigit():
            total = int(cols[3])
        gpus.append(GpuInfo(uuid=cols[0], name=cols[1], memory_total_mib=total))
    return driver, gpus


def inventory_via_nvidia_smi(run: Callable[..., Any] = subprocess.run) -> GpuInventory:
    cmd = ['nvidia-smi', '--query-gpu=uuid,name,driver_version,memory.total', '--format=csv,noheader,nounits']
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=10.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        return GpuInventory(None, source='nvidia-smi', error=repr(e)[:300])
    if proc.returncode != 0:
        return GpuInventory(
            None, source='nvidia-smi', error=(proc.stderr or proc.stdout).strip()[:300] or 'nonzero exit'
        )
    driver, gpus = parse_nvidia_smi_csv(proc.stdout)
    return GpuInventory(driver, gpus, source='nvidia-smi')


def gpu_inventory() -> GpuInventory:
    """NVML first; ``nvidia-smi`` if the library is missing or errors; an empty inventory with the error otherwise."""
    nvml = inventory_via_nvml()
    if nvml.error is None:
        return nvml
    smi = inventory_via_nvidia_smi()
    if smi.error is None:
        return smi
    return GpuInventory(None, source='none', error=f'nvml: {nvml.error}; nvidia-smi: {smi.error}')
