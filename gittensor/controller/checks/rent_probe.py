# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The rent-range probe (vault ``29`` §5): can the controller reach the ports a `gitt up --rent` box says it opened?

A pod's ports are published on the box's own public address, not through our tunnel, so a range the miner's firewall
keeps closed is a rental that fails to start for a customer and a box we would keep offering meanwhile. Rather than
learn that from a customer's ``start_failed``, every full check that sees a rent label starts a throwaway listener on
one port of the range (the agent image the box already runs is an sshd: published with ``-p``, it is the one listener
every box has), dials it from the controller at the box's public address (``BoxState.public_port``: a dev box behind
a remapping host is dialled on the mapped port, as a pod's customer would) and removes it. One port, one connect loop
of a few seconds, once per round.

The probe decides only whether the box is for rent. A range we cannot reach drops ``rent_ports`` from the verdict and
the box is admitted idle-only, exactly like a box started without ``--rent``: it is the miner's configuration, not
fraud, so there is no failed check, no strike and no standing event. The reason travels with the verdict
(``CheckVerdict.rent_probe``), stays on the box (``BoxState.rent_probe``) and reaches the fleet page only as one of
our own phrases (``why.RENT_*``).
"""

from __future__ import annotations

import shlex
import socket
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional, Sequence

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks import why as w
from gittensor.controller.checks.runner import HostRunner

PROBE_LABEL = 'io.gittensor.rent_probe'


@dataclass
class RentProbe:
    """One probe's outcome. ``port`` is the host port the listener was published on (the top of the range: pods take
    the bottom first), ``public_port`` where the controller dialled it, ``reason`` what went wrong in the operator's
    words (it names the address, so it is never published), ``code`` the ``why`` classification the page renders."""

    host: str
    port: int
    public_port: int
    ok: bool
    reason: str = ''
    code: str = ''

    def as_dict(self) -> dict:
        return asdict(self)


def listener_name(port: int) -> str:
    return f'gt-rent-probe-{int(port)}'


def listener_run_command(image_id: str, port: int, ttl_s: float = cfg.RENT_PROBE_LISTENER_TTL_S) -> str:
    """A throwaway sshd on ``port``, published on the box's addresses the way a pod's ports are. The agent image's
    entrypoint is sshd on ``GT_AGENT_SSH_PORT`` with a host key it makes for itself (no volume: forgotten with the
    container); the dev-keys flag is a no-op on a published image and what lets a local build start on a dev box.
    ``timeout`` ends it by itself in case the visit dies before the removal, so a leftover never holds a port a pod
    is later given; a leftover from such a visit is removed first, so its name is free. ``--rm`` cleans up either way."""
    name, p = shlex.quote(listener_name(port)), int(port)
    image = shlex.quote(image_id.removeprefix('sha256:'))
    return (
        f'docker rm -f {name} >/dev/null 2>&1; '
        f'docker run --rm -d --name {name} --label {PROBE_LABEL}=1 -e GT_AGENT_SSH_PORT={p} '
        f'-e GT_AGENT_ALLOW_DEV_KEYS=1 -p {p}:{p} --entrypoint timeout {image} {int(ttl_s)} /entrypoint.sh'
    )


def listener_running_command(port: int) -> str:
    return f"docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(listener_name(port))}"


def listener_rm_command(port: int) -> str:
    return f'docker rm -f {shlex.quote(listener_name(port))}'


def ssh_banner(host: str, port: int, timeout: Optional[float] = None) -> bool:
    """A TCP connect and an SSH banner. The banner matters: with docker's userland proxy a connect to a published port
    succeeds whether or not anything listens behind it."""
    timeout = cfg.RENT_PROBE_DIAL_S if timeout is None else timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).startswith(b'SSH-')
    except (OSError, ValueError):
        return False


def probe_rent_ports(
    runner: HostRunner,
    host: str,
    rent_ports: Sequence[int],
    image_id: str,
    public_port_of: Callable[[int], int] = lambda port: port,
    dial: Optional[Callable[[str, int], bool]] = None,
    timeout_s: Optional[float] = None,
    ssh_timeout_s: float = cfg.SSH_COMMAND_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RentProbe:
    """Start the listener, dial it until the banner answers or ``timeout_s`` (``RENT_PROBE_TIMEOUT_S``) passes, remove
    it. A listener that could not start (no agent image id, docker refused, it exited, the transport died) is its own
    answer, told apart from a closed port: a box that cannot run a container cannot run a pod either."""
    dial = ssh_banner if dial is None else dial
    timeout_s = cfg.RENT_PROBE_TIMEOUT_S if timeout_s is None else timeout_s
    port = int(rent_ports[-1])
    public = int(public_port_of(port))
    if not image_id:
        return RentProbe(host, port, public, False, 'no agent image id to run a listener from', w.RENT_LISTENER_FAILED)
    try:
        started = runner.run(listener_run_command(image_id, port), timeout=ssh_timeout_s)
        if not started.ok:
            detail = (started.stderr or started.stdout).strip()[-200:]
            return RentProbe(
                host, port, public, False, f'the listener would not start: {detail}', w.RENT_LISTENER_FAILED
            )
        deadline = clock() + timeout_s
        while not dial(host, public):
            if clock() >= deadline:
                running = runner.run(listener_running_command(port), timeout=ssh_timeout_s)
                if running.stdout.strip() != 'true':
                    return RentProbe(host, port, public, False, 'the listener exited before it answered', w.RENT_LISTENER_FAILED)  # fmt: skip
                return RentProbe(host, port, public, False, f'{host}:{public} unreachable from the controller (firewall?)', w.RENT_PORT_UNREACHABLE)  # fmt: skip
            sleep(cfg.RENT_PROBE_DIAL_INTERVAL_S)
        return RentProbe(host, port, public, True)
    except Exception as e:  # the transport died mid-probe: nothing was learned about the range
        return RentProbe(host, port, public, False, f'probe failed: {type(e).__name__}: {e}'[:300], w.RENT_LISTENER_FAILED)  # fmt: skip
    finally:
        try:
            runner.run(listener_rm_command(port), timeout=ssh_timeout_s)
        except Exception:
            pass  # best effort: the listener ends itself after its TTL, and the next visit removes the name first
