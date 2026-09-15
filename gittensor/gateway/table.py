# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The instance table: where the healthy leased instances are, as the controller wrote it down (vault ``26`` §2, §4).

Every refresh reads ``<state-dir>/instances.json`` (written atomically by the controller's reconciler) and re-verifies
the registry entry behind each instance; an entry that does not verify leaves its instances unroutable. An instance is
**routable** iff ``healthy and not draining`` and its entry verified: draining flips routing off on the next refresh.
Slots are in-memory counters per instance under the manifest's ``front_door.concurrency``, in one process (Redis only
when a second replica needs shared counters, ``26`` §4). The gateway only reads the state directory.
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gittensor.controller.manifest import Manifest
from gittensor.controller.reconcile import InstanceRecord
from gittensor.controller.registry import Registry, RegistryError

GATEWAY_OPENAI, HTTP = 'gateway-openai', 'http'


@dataclass(frozen=True)
class Instance:
    record: InstanceRecord
    manifest: Manifest | None  # None: its registry entry did not verify on this read
    error: str = ''

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def entry(self) -> str:
        return self.record.entry

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest else self.record.entry.partition('@')[0]

    @property
    def front_door(self) -> str:
        return self.manifest.front_door.type if self.manifest else ''

    @property
    def concurrency(self) -> int:
        # The schema requires front_door.concurrency on HTTP front doors; 1 only guards a manifest without it.
        return max(1, int((self.manifest.front_door.concurrency if self.manifest else None) or 1))

    @property
    def routable(self) -> bool:
        r = self.record
        return self.manifest is not None and r.healthy and not r.draining and bool(r.host) and r.port is not None

    @property
    def base_url(self) -> str:
        host = self.record.host
        return f'http://{f"[{host}]" if ":" in host else host}:{self.record.port}'

    def declares(self, method: str, path: str) -> bool:
        return bool(self.manifest) and any(
            r.method.upper() == method.upper() and r.path == path for r in self.manifest.front_door.routes
        )


@dataclass
class Loaded:
    """One read of the state directory, built off the event loop and swapped in whole."""

    instances: dict[str, Instance] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ''


class InstanceTable:
    def __init__(self, state_dir: str | Path, registry: Registry, rng: random.Random | None = None):
        self.state_dir = Path(state_dir)
        self.registry = registry
        self.rng = rng or random.Random()
        self.instances: dict[str, Instance] = {}
        self.overrides: dict[str, dict[str, Any]] = {}
        self.in_flight: dict[str, int] = {}
        self.runtime_models: dict[str, list[dict[str, Any]]] = {}  # entry id -> the runtime's /v1/models data
        self.loaded_at: float | None = None
        self.last_error = ''

    @property
    def instances_path(self) -> Path:
        return self.state_dir / 'instances.json'

    @property
    def overrides_path(self) -> Path:
        return self.state_dir / 'models_override.json'

    # -- refresh ------------------------------------------------------------------------------------------------------

    def read(self) -> Loaded:
        """Blocking (file reads, one ``ssh-keygen -Y verify`` per entry): run it off the event loop."""
        loaded = Loaded()
        try:
            raw = json.loads(self.instances_path.read_text() or '{}') if self.instances_path.exists() else {}
            records = [InstanceRecord.from_dict(v) for v in raw.values()]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            loaded.error = f'instances.json: {type(e).__name__}: {e}'
            return loaded
        verified: dict[str, tuple[Manifest | None, str]] = {}
        for entry_id in sorted({r.entry for r in records}):
            try:
                verified[entry_id] = (self.registry.read(entry_id).manifest, '')
            except RegistryError as e:
                verified[entry_id] = (None, str(e))
        loaded.instances = {r.id: Instance(r, *verified[r.entry]) for r in records}
        try:
            if self.overrides_path.exists():
                overrides = json.loads(self.overrides_path.read_text() or '{}')
                loaded.overrides = {str(k): v for k, v in overrides.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError) as e:
            loaded.error = f'models_override.json: {type(e).__name__}: {e}'
        return loaded

    def apply(self, loaded: Loaded, now: float) -> None:
        """Swap a read in. A broken ``instances.json`` keeps the previous table (the controller writes atomically, so
        that is a bug or an operator edit, not a half-written file)."""
        self.last_error = loaded.error
        if loaded.error.startswith('instances.json'):
            return
        self.instances = loaded.instances
        self.overrides = loaded.overrides
        self.loaded_at = now
        entries = {i.entry for i in self.instances.values()}
        self.runtime_models = {e: data for e, data in self.runtime_models.items() if e in entries}
        self.in_flight = {i: n for i, n in self.in_flight.items() if n > 0}

    # -- routing ------------------------------------------------------------------------------------------------------

    def runtime_ids(self, entry_id: str) -> list[str]:
        return [str(m['id']) for m in self.runtime_models.get(entry_id, []) if isinstance(m, dict) and m.get('id')]

    def openai_candidates(self, model: str) -> list[Instance]:
        """Instances of every entry that serves ``model``: its manifest name, its entry id, or an id its runtime
        reports. An unverified entry matches by the name in its id, so its instances read as full, not unknown."""
        return [
            i
            for i in self.instances.values()
            if i.front_door in (GATEWAY_OPENAI, '') and model in (i.name, i.entry, *self.runtime_ids(i.entry))
        ]

    def http_candidates(self, name: str) -> list[Instance]:
        return [i for i in self.instances.values() if i.front_door in (HTTP, '') and i.name == name]

    def acquire(self, candidates: list[Instance]) -> Instance | None:
        """A routable instance with a free slot, least loaded first, random tie-break; the slot is taken before this
        returns (one event loop: no await between the check and the count). None: 429, never queue."""
        free = [i for i in candidates if i.routable and self.in_flight.get(i.id, 0) < i.concurrency]
        if not free:
            return None
        low = min(self.in_flight.get(i.id, 0) for i in free)
        picked = self.rng.choice(sorted((i for i in free if self.in_flight.get(i.id, 0) == low), key=lambda i: i.id))
        self.in_flight[picked.id] = self.in_flight.get(picked.id, 0) + 1
        return picked

    def release(self, instance_id: str) -> None:
        left = self.in_flight.get(instance_id, 0) - 1
        if left > 0:
            self.in_flight[instance_id] = left
        else:
            self.in_flight.pop(instance_id, None)

    def runtime_model_for(self, instance: Instance, model: str) -> str | None:
        """The id to put in the forwarded body when the client named the entry (manifest name or entry id) and the
        runtime serves exactly one model under another id; None leaves ``model`` as sent."""
        ids = self.runtime_ids(instance.entry)
        if model in (instance.name, instance.entry) and len(ids) == 1 and ids[0] != model:
            return ids[0]
        return None

    # -- what /v1/models and /healthz publish ------------------------------------------------------------------------

    def routable_counts(self) -> dict[str, int]:
        counts = {i.entry: 0 for i in self.instances.values()}
        for i in self.instances.values():
            counts[i.entry] += int(i.routable)
        return dict(sorted(counts.items()))

    def models_source(self) -> dict[str, list[Instance]]:
        """Per gateway-openai entry, its routable instances: where ``/v1/models`` is fetched from."""
        out: dict[str, list[Instance]] = {}
        for i in self.instances.values():
            if i.front_door == GATEWAY_OPENAI and i.routable:
                out.setdefault(i.entry, []).append(i)
        return out

    def model_objects(self, cap: int) -> list[dict[str, Any]]:
        """One object per model name with a routable instance: the runtime's own object (capabilities kept) with the
        manifest name as ``id``, then the operator override (keyed by name, then by entry id) merged on top."""
        by_name: dict[str, str] = {}
        for entry_id in sorted(self.models_source(), key=_entry_order):
            by_name.setdefault(entry_id.partition('@')[0], entry_id)  # highest version of a name wins
        out = []
        for name, entry_id in sorted(by_name.items()):
            data = [m for m in self.runtime_models.get(entry_id, []) if isinstance(m, dict)]
            runtime = next((m for m in data if m.get('id') == name), data[0] if data else {})
            obj = {**copy.deepcopy(runtime), 'id': name, 'object': 'model', 'owned_by': 'gittensor'}
            limit = obj.get('max_output_tokens')
            obj['max_output_tokens'] = min(limit, cap) if isinstance(limit, int) and limit > 0 else cap
            for key in (name, entry_id):
                _merge(obj, {k: v for k, v in self.overrides.get(key, {}).items() if k != 'id'})
            out.append(obj)
        return out


def _entry_order(entry_id: str) -> tuple[str, int]:
    name, _, version = entry_id.partition('@')
    return name, -int(version) if version.isdigit() else 0


def _merge(into: dict[str, Any], over: dict[str, Any]) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value
