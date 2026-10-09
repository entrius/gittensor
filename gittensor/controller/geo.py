# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Where a box is (host specs, 10/9): a free, keyless lookup of its public address, cached on the box record.

Controller side, never on the round's path: ``refresh_locations`` is a pure pass over the boxes that are due (no
location yet, a week since the last one, a day since a miss) and returns the new records for the caller to write;
``lookup_ip`` is the one network call, bounded by ``GEO_TIMEOUT_S`` and ``GEO_PER_PASS``. A private address (a dev
box) is never looked up. What comes back is third-party text, so ``publish`` holds every field to a pattern before it
reaches the page; here it is only clipped.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import BoxState

UNKNOWN = 'unknown'
Lookup = Callable[[str], 'dict[str, str] | None']


def is_public(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False  # a hostname: not looked up (the box's published endpoint is an address)
    return address.is_global


def lookup_ip(ip: str, opener=urllib.request.urlopen, timeout: float = cfg.GEO_TIMEOUT_S) -> dict[str, str] | None:
    """``{'country': ISO-3166 alpha-2, 'region', 'city'}`` for a public address, None for anything else (a private
    address, no answer, a provider error). Never raises."""
    if not is_public(ip):
        return None
    req = urllib.request.Request(cfg.GEO_URL.format(ip=ip), headers={'Accept': 'application/json'})
    try:
        with opener(req, timeout=timeout) as resp:
            doc = json.loads(resp.read() or b'{}')
    except Exception:
        return None
    if not isinstance(doc, dict) or doc.get('status') != 'success':
        return None
    country = str(doc.get('countryCode') or '').strip().upper()
    if len(country) != 2 or not country.isalpha():
        return None  # not an alpha-2 code: no guess ('USA' is not 'US')
    return {
        'country': country,
        'region': ' '.join(str(doc.get('regionName') or '').split())[:64],
        'city': ' '.join(str(doc.get('city') or '').split())[:64],
    }


def location_due(
    box: BoxState, now: float, refresh_s: float = cfg.GEO_REFRESH_S, retry_s: float = cfg.GEO_RETRY_S
) -> bool:
    """A box with an address and no location, one older than ``refresh_s``, or a miss older than ``retry_s``."""
    if not box.host or not is_public(box.host):
        return False
    at = box.location.get('at')
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True
    missed = box.location.get('country') in (None, '', UNKNOWN)
    return now - float(at) >= (retry_s if missed else refresh_s)


def refresh_locations(
    boxes: Mapping[str, BoxState], now: float, lookup: Lookup = lookup_ip, per_pass: int = cfg.GEO_PER_PASS
) -> dict[str, dict[str, Any]]:
    """The new ``location`` record per box that was due, at most ``per_pass`` lookups (the oldest first). A miss is
    recorded as ``unknown`` with its time, so it is retried after ``GEO_RETRY_S`` and not every pass. Pure."""
    due = [b for b in boxes.values() if location_due(b, now)]
    due.sort(key=lambda b: float(b.location.get('at') or 0.0))  # type: ignore[arg-type]
    out: dict[str, dict[str, Any]] = {}
    for box in due[:per_pass]:
        found = lookup(box.host)
        record: dict[str, Any] = {'at': now, 'ip': box.host}
        record.update(found if found else {'country': UNKNOWN, 'region': '', 'city': ''})
        out[box.box_id] = record
    return out
