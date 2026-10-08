#!/bin/bash
# Gittensor compute — Sysbox setup for a rentable box (`gitt up --rent`, vault 29 §5).
#
# A customer's pod runs under Sysbox (`docker run --runtime=sysbox-runc`), never `--privileged`: root, docker and
# systemd work inside the pod without host root. This installs Sysbox CE, pinned by version and checksum, registers the
# runtime with Docker and verifies a GPU pod starts. The same shape as Lium's executor setup
# (Datura-ai/lium-io neurons/executor/nvidia_docker_sysbox_setup.sh), reduced to what `gitt up --rent` checks.
#
#   curl -fsSL https://raw.githubusercontent.com/entrius/gittensor/main/docker/agent/sysbox-setup.sh | sudo bash
#   sudo bash sysbox-setup.sh --check     # the preflight only; exit 1 on anything that would stop the install
#
# Requirements: Ubuntu 22.04+ / Debian 12+ (amd64), kernel 5.19+ (overlayfs over ID-mapped mounts), Docker with the
# NVIDIA container toolkit, and NO containers running: the Sysbox package refuses to install beside any. Run `gitt down`
# first; `gitt up --rent` afterwards. Docker is restarted once.
set -euo pipefail

SYSBOX_VERSION="0.7.1"  # keep with gittensor/agent/config.py SYSBOX_VERSION
SYSBOX_DEB="sysbox-ce_${SYSBOX_VERSION}.linux_amd64.deb"
SYSBOX_URL="https://github.com/nestybox/sysbox/releases/download/v${SYSBOX_VERSION}/${SYSBOX_DEB}"
SYSBOX_SHA256="9d6d5484f980d0a17f86c492c1262015c2afb66280bdb97215b79fde6a0261c5"  # verified 2026-10-06
KERNEL_MIN_MAJOR=5
KERNEL_MIN_MINOR=19
VERIFY_IMAGE="${GT_SYSBOX_VERIFY_IMAGE:-nvidia/cuda:12.8.0-base-ubuntu24.04}"
# An AMD box (vault 30 §1 #5): no container toolkit, the cards are device nodes. The verify pod gets /dev/kfd and the
# render nodes, as a customer pod does. Pin the tag once the MI300X run (31 step 1) has used it.
VERIFY_IMAGE_AMD="${GT_SYSBOX_VERIFY_IMAGE_AMD:-rocm/rocm-terminal:6.4}"
VENDOR=nvidia
[ -d /sys/module/amdgpu ] && [ ! -d /sys/module/nvidia ] && VENDOR=amd
CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[0;31m✗\033[0m %s\n' "$1" >&2; }
die()  { fail "$1"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)."
[ "$(uname -m)" = "x86_64" ] || die "Sysbox CE ships for amd64 only; this box is $(uname -m)."
command -v apt-get >/dev/null || die "apt-get not found: this script covers Ubuntu / Debian."

echo "Sysbox ${SYSBOX_VERSION} for a rentable Gittensor box"

# ── preflight ───────────────────────────────────────────────────────────────────────────────────────────────────────
kernel="$(uname -r)"
major="${kernel%%.*}"; rest="${kernel#*.}"; minor="${rest%%[.-]*}"
if [ "$major" -lt "$KERNEL_MIN_MAJOR" ] || { [ "$major" -eq "$KERNEL_MIN_MAJOR" ] && [ "$minor" -lt "$KERNEL_MIN_MINOR" ]; }; then
    die "kernel $kernel is older than ${KERNEL_MIN_MAJOR}.${KERNEL_MIN_MINOR}: Sysbox needs overlayfs over ID-mapped mounts (Ubuntu: apt-get install linux-generic-hwe-22.04 && reboot)."
fi
ok "kernel $kernel"

docker ps >/dev/null 2>&1 || die "Docker is not running (systemctl enable --now docker)."
server="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
ok "Docker ${server:-?}"
if [ "$VENDOR" = amd ]; then
    [ -e /dev/kfd ] || die "/dev/kfd is missing: the amdgpu driver is loaded but KFD is not up (reboot, or check dmesg for amdgpu)."
    ok "AMD driver loaded (amdgpu; cards attach as device nodes, no container toolkit needed)"
else
    if ! docker info --format '{{json .Runtimes}}' | grep -q '"nvidia"' && ! command -v nvidia-container-runtime >/dev/null; then
        die "the NVIDIA container toolkit is not installed (nvidia-container-runtime missing): install it first, then re-run."
    fi
    ok "NVIDIA container toolkit"
    nvidia-smi -L >/dev/null 2>&1 || die "nvidia-smi does not answer: the NVIDIA driver is not loaded."
    ok "NVIDIA driver loaded"
fi

if docker info --format '{{json .Runtimes}}' | grep -q '"sysbox-runc"'; then
    ok "sysbox-runc is already registered with Docker"
    INSTALLED=true
else
    INSTALLED=false
fi
running="$(docker ps -q | wc -l)"
if [ "$INSTALLED" = false ] && [ "$running" -gt 0 ]; then
    die "$running container(s) running; the Sysbox package installs only on a box with none. Run \`gitt down\` (and stop anything else) first."
fi

if [ "$CHECK_ONLY" = true ]; then
    ok "preflight passed"
    exit 0
fi

# ── install ────────────────────────────────────────────────────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq jq fuse3 >/dev/null  # sysbox-fs needs fusermount3 and the .deb does not pull it in
if [ "$INSTALLED" = false ]; then
    # The package refuses to install beside any container, running or stopped.
    stopped="$(docker ps -aq)"
    if [ -n "$stopped" ]; then
        warn "removing $(echo "$stopped" | wc -l) stopped container(s): the Sysbox package installs on a box with none"
        # shellcheck disable=SC2086
        docker rm -f $stopped >/dev/null
    fi
    deb="$(mktemp /tmp/sysbox-ce.XXXXXX.deb)"
    trap 'rm -f "$deb"' EXIT
    echo "  downloading $SYSBOX_DEB"
    curl -fsSL -o "$deb" "$SYSBOX_URL"
    actual="$(sha256sum "$deb" | cut -d' ' -f1)"
    [ "$actual" = "$SYSBOX_SHA256" ] || die "checksum mismatch for $SYSBOX_DEB: got $actual, expected $SYSBOX_SHA256. Nothing installed."
    ok "checksum verified"
    apt-get install -y -qq "$deb" >/dev/null
    ok "Sysbox $SYSBOX_VERSION installed"
fi

# ── docker daemon.json: register the runtime; CDI (and time namespaces on 29.5+) off, sysbox-runc rejects both ───────
patch='{"runtimes":{"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}},"features":{"cdi":false}}'
if [ -n "$server" ] && [ "$(printf '%s\n29.5.0\n' "$server" | sort -V | head -1)" = "29.5.0" ]; then
    patch="$(echo "$patch" | jq -c '.features["time-namespaces"] = false')"
fi
mkdir -p /etc/docker
if [ -f /etc/docker/daemon.json ]; then
    cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
    jq --argjson p "$patch" '. * $p' /etc/docker/daemon.json > /etc/docker/daemon.json.tmp \
        || die "/etc/docker/daemon.json is not valid JSON; fix it (backup: daemon.json.bak) and re-run."
    mv /etc/docker/daemon.json.tmp /etc/docker/daemon.json
    ok "merged into /etc/docker/daemon.json (backup: daemon.json.bak)"
else
    echo "$patch" | jq . > /etc/docker/daemon.json
    ok "wrote /etc/docker/daemon.json"
fi
systemctl restart docker
for _ in $(seq 1 30); do docker ps >/dev/null 2>&1 && break; sleep 1; done
docker ps >/dev/null 2>&1 || die "Docker did not come back after the restart: journalctl -u docker.service"
ok "Docker restarted"

# ── verify: a GPU pod under sysbox-runc ─────────────────────────────────────────────────────────────────────────────
if [ "$VENDOR" = amd ]; then
    nodes=""
    for n in /dev/dri/renderD*; do [ -e "$n" ] && nodes="$nodes --device $n"; done
    # shellcheck disable=SC2086  # $nodes is a list of --device flags built from the box's own /dev/dri
    verify="docker run --rm --runtime=sysbox-runc --device /dev/kfd $nodes --group-add video --group-add render $VERIFY_IMAGE_AMD rocminfo"
else
    verify="docker run --rm --runtime=sysbox-runc --gpus all $VERIFY_IMAGE nvidia-smi -L"
fi
if $verify >/dev/null 2>&1; then
    ok "a GPU pod starts under sysbox-runc"
    echo
    echo "Done. Start the box with:  gitt up --rent   (open the rent ports on your firewall; default 31000-31099)"
else
    die "a pod did not start under sysbox-runc with the GPUs. Check: $verify"
fi
