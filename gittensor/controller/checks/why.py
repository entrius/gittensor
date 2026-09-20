# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Why a box is benched, in words a miner can act on — and safe to publish.

``controller.jsonl`` already carries the full reason a check failed (``cli.check_detail`` over
``evidence['reason']``), but that reason embeds what the box reported: a ``/proc/<pid>/comm``, a PID, a container ID,
a path, an image digest. ``public/fleet.json`` is served to the whole internet, so none of it may go there — a comm is
15 bytes of anything the miner chooses, which makes the obvious "just publish the reason" a defacement vector, an XSS
vector the day the page renders unescaped, and a window onto a miner's machine.

So a check does not hand us a string to sanitize. It **classifies** what it saw into a ``code`` from the closed
vocabulary below plus a few integers, writing that under ``evidence[PUBLIC]``; ``render`` turns the pair into a
phrase by formatting one of our own templates with those integers. The invariant that follows is the whole point of
this module, and it is checked by reading it:

    A published phrase is ``PHRASES[code]`` — a string constant in this file — formatted with integers.
    No substring of anything a box reported can reach it.

``render`` enforces it rather than assuming it: the code is only ever used as a key into ``PHRASES`` (an unknown one
renders nothing), and every other value is passed through ``int()`` (a string is dropped). A check that writes text
into ``evidence[PUBLIC]`` therefore cannot publish it even by accident. ``publish.py`` re-checks the rendered phrase
against a tight pattern on the way out, so the two layers have to fail together.

Where a number would leak capacity or configuration, the phrase names our constant instead of their reading: "below
the 50 GB floor", never "4.1 GB free".
"""

from collections import Counter
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

PUBLIC = 'public'  # the evidence key a check writes its classification under: {'code': ..., <name>: <int>, ...}

# ---------------------------------------------------------------- the closed vocabulary -----------------------------

# card_free: who is holding an NVIDIA device node that is not one of our containers.
DESKTOP_SESSION = 'desktop_session'
ANOTHER_CONTAINER = 'another_container'
GPU_WORKLOAD = 'gpu_workload'
HOST_PROCESS = 'host_process'  # the generic bucket: on the host, in no container, comm we do not recognise
UNREADABLE_HOLDER = 'unreadable_holder'
CARD_FREE_UNSCANNABLE = 'card_free_unscannable'

# gpu_proof
PROOF_STAGING = 'proof_staging'
PROOF_NO_ANSWER = 'proof_no_answer'
PROOF_RUNTIME_NVIDIA = 'proof_runtime_nvidia'
PROOF_CONTAINER = 'proof_container'
PROOF_WRONG_CARD = 'proof_wrong_card'
PROOF_BAD_ANSWER = 'proof_bad_answer'

# disk_free
DISK_BELOW_FLOOR = 'disk_below_floor'
DISK_UNREADABLE = 'disk_unreadable'

# agent_image
AGENT_IMAGE_UNPINNED = 'agent_image_unpinned'
AGENT_IMAGE_MISMATCH = 'agent_image_mismatch'
AGENT_IMAGE_LOCAL_BUILD = 'agent_image_local_build'
AGENT_IMAGE_UNREADABLE = 'agent_image_unreadable'

# nvml_digest
NVML_DRIVER_MISSING = 'nvml_driver_missing'
NVML_DRIVER_DISAGREES = 'nvml_driver_disagrees'
NVML_LIBRARY_MISSING = 'nvml_library_missing'
NVML_DRIVER_UNKNOWN = 'nvml_driver_unknown'
NVML_DIGEST_MISMATCH = 'nvml_digest_mismatch'

# gpu_spec
SPEC_UNREADABLE = 'spec_unreadable'
SPEC_CARD_COUNT = 'spec_card_count'
SPEC_MODEL = 'spec_model'
SPEC_COMPUTE_CAP = 'spec_compute_cap'
SPEC_VRAM = 'spec_vram'
SPEC_BAD_UUID = 'spec_bad_uuid'

# gpu_uuid_pin
UUID_DUPLICATE = 'uuid_duplicate'
UUID_CHANGED = 'uuid_changed'

# power_limit
POWER_NO_GPUS = 'power_no_gpus'
POWER_BELOW_FLOOR = 'power_below_floor'
POWER_UNREPORTED = 'power_unreported'

# fleet_uuid_unique
UUID_CLAIMED_ELSEWHERE = 'uuid_claimed_elsewhere'


PHRASES: Dict[str, str] = {
    # card_free. ``n`` is every foreign holder, whatever bucket it landed in; the code is the bucket that holds most
    # of them, so the phrase names the one thing most worth acting on and the count still adds up.
    DESKTOP_SESSION: 'a desktop session is using this GPU ({n} processes outside our containers)',
    ANOTHER_CONTAINER: 'a container we did not start is using this GPU ({n} processes outside our containers)',
    GPU_WORKLOAD: 'another GPU workload is running on this box ({n} processes outside our containers)',
    HOST_PROCESS: 'another process on this host is using this GPU ({n} processes outside our containers)',
    UNREADABLE_HOLDER: 'something is holding this GPU and we could not tell what ({n} processes outside our containers)',  # noqa: E501
    CARD_FREE_UNSCANNABLE: 'we could not scan this box for processes holding the GPU',
    # gpu_proof
    PROOF_STAGING: 'we could not set up the GPU proof on this box',
    PROOF_NO_ANSWER: 'no card answered the GPU proof',
    PROOF_RUNTIME_NVIDIA: 'the NVIDIA container runtime would not start our GPU proof container ({n} cards)',
    PROOF_CONTAINER: 'our GPU proof container would not start on this box ({n} cards)',
    PROOF_WRONG_CARD: 'a card answered the GPU proof for a different GPU ({n} cards)',
    PROOF_BAD_ANSWER: 'the GPU proof did not check out on {n} card(s) of this box',
    # disk_free
    DISK_BELOW_FLOOR: 'free disk space is below the {floor_gb} GB floor on the disk Docker uses',
    DISK_UNREADABLE: 'we could not read how much disk space is free on this box',
    # agent_image
    AGENT_IMAGE_UNPINNED: 'no agent image is pinned in the pool config, so nothing can be admitted (our side)',
    AGENT_IMAGE_MISMATCH: 'this box runs an agent image we did not publish',
    AGENT_IMAGE_LOCAL_BUILD: "this box's agent image is a local build with no published digest",
    AGENT_IMAGE_UNREADABLE: 'we could not read which agent image this box runs',
    # nvml_digest
    NVML_DRIVER_MISSING: 'this box did not report an NVIDIA driver version',
    NVML_DRIVER_DISAGREES: 'nvidia-smi and the kernel module report different NVIDIA driver versions',
    NVML_LIBRARY_MISSING: "we could not find or hash this box's NVIDIA management library",
    NVML_DRIVER_UNKNOWN: "this box's NVIDIA driver version is not on our vetted list yet",
    NVML_DIGEST_MISMATCH: "this box's NVIDIA management library is not the one published for its driver",
    # gpu_spec
    SPEC_UNREADABLE: 'nvidia-smi did not answer on this box',
    SPEC_CARD_COUNT: 'this box reports {n} GPUs, the pool admits {low} to {high}',
    SPEC_MODEL: 'the GPU model on this box is not the one the pool admits ({n} cards)',
    SPEC_COMPUTE_CAP: 'the GPU compute capability on this box is not the one the pool admits ({n} cards)',
    SPEC_VRAM: 'the GPU memory on this box is outside the range the pool admits ({n} cards)',
    SPEC_BAD_UUID: 'this box reported a malformed GPU UUID ({n} cards)',
    # gpu_uuid_pin
    UUID_DUPLICATE: 'this box reported the same GPU UUID twice',
    UUID_CHANGED: 'the GPUs on this box are not the ones pinned when it was admitted ({missing} gone, {extra} new)',
    # power_limit
    POWER_NO_GPUS: 'this box reported no GPUs',
    POWER_BELOW_FLOOR: 'the power limit is set below the pool floor on {n} card(s)',
    POWER_UNREPORTED: 'this box did not report a power limit on {n} card(s)',
    # fleet_uuid_unique
    UUID_CLAIMED_ELSEWHERE: 'another box in the pool claims {n} of the GPUs this box reports',
}

# A failure with nothing to classify: the name alone says it, so the phrase is fixed per name. Keyed by what
# ``BoxState.last_failed`` holds — including the ``heartbeat:`` prefix an in-lease failure is stored under.
BY_NAME: Dict[str, str] = {
    'ssh_unreachable': "this box's agent did not answer for several rounds",
    'deregistered': 'this hotkey is no longer registered on the subnet',
    'failed_starts': 'our workload failed to start on this box several times in a row',
    'external_use': 'the GPU we were paying for was doing work that was not ours',
    'heartbeat:same_card': 'the GPU under our workload changed while it was leased',
    'heartbeat:our_container': 'our workload container was not running on this box',
    'heartbeat:card_ours_alone': 'something outside our workload was using this GPU while it was leased',
    # The check names, for a failure a check could not classify further (it wrote no ``evidence[PUBLIC]``).
    'card_free': 'something outside our containers is using this GPU',
    'gpu_proof': 'this box did not pass the GPU proof',
    'disk_free': 'this box does not have enough free disk space',
    'agent_image': 'the agent image on this box is not one we published',
    'nvml_digest': 'the NVIDIA driver and library on this box did not check out',
    'gpu_spec': 'the GPUs on this box do not match the pool spec',
    'gpu_uuid_pin': 'the GPUs on this box are not the ones pinned when it was admitted',
    'power_limit': 'the GPU power limit on this box is below the pool floor',
    'fleet_uuid_unique': 'another box in the pool claims a GPU this box reports',
    'network': 'this box could not reach one of the endpoints the pool needs',
}


# ---------------------------------------------------------------- rendering (the invariant lives here) --------------


def render(public: Optional[Mapping[str, Any]]) -> str:
    """One classification as a published phrase, or ``''``.

    The only two things that reach the output are ``PHRASES[code]`` and integers. ``code`` is used as a dict key and
    never printed, so an unrecognised one renders nothing rather than leaking itself; every other value goes through
    ``int()``, so a string a check put there by mistake is dropped instead of published. This is the enforcement
    point for the no-box-substring rule, not a convention callers are trusted to keep."""
    if not isinstance(public, Mapping):
        return ''
    code = public.get('code')
    template = PHRASES.get(code) if isinstance(code, str) else None
    if template is None:
        return ''
    numbers: Dict[str, int] = {}
    for key, value in public.items():
        if key == 'code' or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        numbers[str(key)] = int(value)
    try:
        return template.format(**numbers)
    except (KeyError, IndexError, ValueError):
        return ''


def phrase(name: str, evidence: Optional[Mapping[str, Any]] = None) -> str:
    """The published phrase for one failed check: its classification if it made one, else the fixed phrase for the
    name, else ``''`` (a name we have no words for — the UI falls back to naming the check)."""
    return render((evidence or {}).get(PUBLIC)) or BY_NAME.get(name, '')


def from_results(results: Iterable[Any]) -> Dict[str, str]:
    """``{check name: phrase}`` over ``CheckResult``s — every one that did not pass, whether it failed or could not
    be carried out. Names with no phrase are left out."""
    out: Dict[str, str] = {}
    for result in results:
        if getattr(result, 'passed', True) or getattr(result, 'skipped', False):
            continue
        text = phrase(result.name, getattr(result, 'evidence', None))
        if text:
            out[result.name] = text
    return out


def for_names(names: Sequence[str]) -> Dict[str, str]:
    """``{name: phrase}`` for failures that carry no evidence — a heartbeat bench, an unreachable box, a
    deregistration, too many failed starts, external use."""
    return {name: BY_NAME[name] for name in names if name in BY_NAME}


# ---------------------------------------------------------------- classifying what the checks saw --------------------

# ``/proc/<pid>/comm`` stops at 15 bytes, so these are prefixes, matched against the truncated value. A miner controls
# the comm and can land their process in whichever bucket they like; that changes the words on their own fleet row and
# nothing else — the bench is the same either way, and no part of the comm is published.
DESKTOP_COMMS = (
    'Xorg',
    'Xwayland',
    'gnome-',
    'gdm-',
    'kwin',
    'mutter',
    'plasmashell',
    'sddm',
    'lightdm',
    'xfwm',
    'cinnamon',
    'budgie',
    'weston',
    'wayfire',
    'sway',
    'Hyprland',
    'picom',
    'compton',
    'xdg-desktop-po',
    'snapd-desktop-',
)

GPU_WORKLOAD_COMMS = (
    'python',
    'pt_main_thread',
    'ollama',
    'vllm',
    'sglang',
    'llama-',
    'ComfyUI',
    'tritonserver',
    'ray::',
    'nvidia-cuda-mp',
    'xmrig',
    't-rex',
    'trex',
    'lolMiner',
    'gminer',
    'nbminer',
    'ethminer',
    'phoenixminer',
)

# When foreign holders fall into more than one bucket the biggest wins; this breaks the tie, most specific first.
_HOLDER_ORDER = (ANOTHER_CONTAINER, DESKTOP_SESSION, GPU_WORKLOAD, HOST_PROCESS, UNREADABLE_HOLDER)


def holder_category(comm: str, containers: Optional[Iterable[str]], read: bool) -> str:
    """One foreign device holder as a bucket. ``containers`` is the container IDs in its cgroup (already known not to
    be ours), ``read`` whether its comm and cgroup came back at all. Nothing is derived from the comm except which
    of our own prefixes it starts with: an unrecognised one is ``HOST_PROCESS``, never a category of its own."""
    if not read:
        return UNREADABLE_HOLDER
    if containers:
        return ANOTHER_CONTAINER
    name = (comm or '')[:15]
    if name.startswith(DESKTOP_COMMS):
        return DESKTOP_SESSION
    if name.startswith(GPU_WORKLOAD_COMMS):
        return GPU_WORKLOAD
    return HOST_PROCESS


def card_free_public(categories: Sequence[str]) -> Dict[str, Any]:
    """The foreign holders' buckets as one classification: the bucket with the most holders (ties by
    ``_HOLDER_ORDER``) and how many foreign holders there were altogether."""
    counts = Counter(categories)
    code = min(counts, key=lambda c: (-counts[c], _HOLDER_ORDER.index(c) if c in _HOLDER_ORDER else len(_HOLDER_ORDER)))
    return {'code': code, 'n': len(categories)}
