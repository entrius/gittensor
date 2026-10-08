#!/usr/bin/env bash
# Capture what the controller's AMD checks read, on a real AMD box, as the fixtures the tests hold the parsers to
# (vault 31 step 1 item 1; 33 is the runbook). Run as root on the box; writes <out>/ with:
#   sysfs_<label>.txt            exactly AMD_SYSFS_COMMAND's output (checks/amd_scrape.py) -> replaces the synthetic one
#   device_holders_<label>.txt   exactly AMD_DEVICE_HOLDERS_COMMAND's output at rest (must be empty)
#   raw/                         every sysfs file the parsers assume, KFD topology, dmesg, rocminfo, rocm-smi, ls -l
#   stack.txt                    kernel, amdgpu module version, ROCm packages, docker, sysbox
#
#   sudo tests/controller/fixtures/amd/capture.sh mi300x_1 /root/amd-capture
#
# Needs the gittensor checkout on PYTHONPATH (run from the repo root, or set GT_REPO).
set -euo pipefail
label="${1:?label, e.g. mi300x_1}"
out="${2:-$PWD/amd-capture}"
repo="${GT_REPO:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
mkdir -p "$out/raw"
cmd() { PYTHONPATH="$repo" python3 -c "from gittensor.controller.checks.$1 import $2; print($2)"; }

sh -c "$(cmd amd_scrape AMD_SYSFS_COMMAND)" > "$out/sysfs_$label.txt"
# the holder scan runs inside the agent against the host's /proc at HOST_ROOT; here it is the box's own /proc
sh -c "$(cmd scrape AMD_DEVICE_HOLDERS_COMMAND | sed 's#^H=[^;]*/proc;#H=/proc;#')" > "$out/device_holders_$label.txt" || true

for d in /sys/class/drm/renderD*/device; do
    n="$(basename "$(dirname "$d")")"
    [ "$(cat "$d/vendor" 2>/dev/null)" = 0x1002 ] || continue
    mkdir -p "$out/raw/$n"
    for f in unique_id vendor device subsystem_vendor subsystem_device product_name product_number serial_number \
             mem_info_vram_total mem_info_vram_used mem_info_vis_vram_total current_compute_partition \
             current_memory_partition available_compute_partition vbios_version unique_id pcie_bw numa_node; do
        [ -e "$d/$f" ] && cp "$d/$f" "$out/raw/$n/$f" 2>/dev/null || true
    done
    ls -l "$d/" > "$out/raw/$n/ls.txt" 2>&1 || true
    [ -d "$d/fw_version" ] && { mkdir -p "$out/raw/$n/fw_version"; cp "$d"/fw_version/* "$out/raw/$n/fw_version/" 2>/dev/null || true; }
    for h in "$d"/hwmon/hwmon*; do [ -d "$h" ] || continue; mkdir -p "$out/raw/$n/hwmon"
        for f in "$h"/power1_* "$h"/temp*_input "$h"/name; do [ -e "$f" ] && cp "$f" "$out/raw/$n/hwmon/" 2>/dev/null || true; done; done
    readlink -f "$d" > "$out/raw/$n/pci.txt"
done
mkdir -p "$out/raw/kfd"
for p in /sys/class/kfd/kfd/topology/nodes/*/properties; do
    [ -f "$p" ] && cp "$p" "$out/raw/kfd/node_$(basename "$(dirname "$p")").properties"
done
ls -l /dev/kfd /dev/dri > "$out/raw/dev_ls.txt" 2>&1 || true
{ echo "kernel=$(uname -r)"; echo "amdgpu_module=$(cat /sys/module/amdgpu/version 2>/dev/null || echo in-tree)"
  echo "os=$(. /etc/os-release && echo "$PRETTY_NAME")"; echo "docker=$(docker --version 2>&1 || true)"
  echo "sysbox=$(sysbox-runc --version 2>&1 | head -1 || true)"; echo "rocm_pkgs:"; dpkg -l 2>/dev/null | grep -Ei 'rocm|amdgpu|hip-runtime|hsa-rocr' | awk '{print "  "$2" "$3}'
} > "$out/stack.txt"
command -v rocminfo >/dev/null && rocminfo > "$out/raw/rocminfo.txt" 2>&1 || true
command -v rocm-smi >/dev/null && rocm-smi --showuniqueid --showserial --showproductname --showmemuse --showpower --showcomputepartition --showmemorypartition > "$out/raw/rocm-smi.txt" 2>&1 || true
dmesg 2>/dev/null | grep -i amdgpu > "$out/raw/dmesg_amdgpu.txt" || true
echo "captured to $out"; echo "--- sysfs_$label.txt ---"; cat "$out/sysfs_$label.txt"
echo "--- device holders at rest (expect nothing) ---"; cat "$out/device_holders_$label.txt"
