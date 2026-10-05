# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The image manifest: what every blessed image tells the controller about itself (vault ``25``; the public spec is
``entrius/gittensor-compute-template`` ``MANIFEST.md`` + ``manifest.schema.json``, which wins where they differ).

``manifest.schema.json`` beside this module is a byte-for-byte copy of the template's schema. ``parse_manifest``
validates a parsed document against it, then runs the consistency checks the schema cannot express (the same ones
the template's ``scripts/validate`` applies), and returns a typed ``Manifest``. The controller never reads a manifest
from a miner box: only from a verified registry entry (``registry.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).with_name('manifest.schema.json')
_DIGEST = re.compile(r'@(sha256:[a-f0-9]{64})$')
_ZERO_DIGEST = 'sha256:' + '0' * 64
_HTTP_FRONT_DOORS = ('gateway-openai', 'http')
_OPENAI_ROUTES = ('/v1/chat/completions', '/v1/models')

# nvidia-smi card name -> manifest `placement.gpu_types` name. Unknown names normalise to their alphanumerics with the
# vendor words dropped ("NVIDIA H100 80GB HBM" -> "H10080GBHBM"), which matches nothing a manifest lists today.
GPU_TYPE_NAMES = {'NVIDIA GeForce RTX 5090': 'RTX5090'}


class ManifestError(ValueError):
    """The manifest fails the schema or a consistency check. ``problems`` lists every one."""

    def __init__(self, problems: list[str]):
        super().__init__('; '.join(problems))
        self.problems = problems


@dataclass(frozen=True)
class GpuTypes:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def admits(self, gpu_type: str) -> bool:
        if self.include and gpu_type not in self.include:
            return False
        return gpu_type not in self.exclude


@dataclass(frozen=True)
class Placement:
    gpu_types: GpuTypes
    cards_per_instance: int
    min_vram_gb: float
    max_load_s: int


@dataclass(frozen=True)
class Volume:
    name: str
    mount: str
    read_only: bool = False


@dataclass(frozen=True)
class Run:
    env: dict[str, str] = field(default_factory=dict)
    volumes: tuple[Volume, ...] = ()


@dataclass(frozen=True)
class Artifact:
    path: str
    source: str
    revision: str
    sha256: str


@dataclass(frozen=True)
class HttpProbe:
    path: str
    port: int
    expect_status: int = 200


@dataclass(frozen=True)
class Health:
    interval_s: int
    failure_threshold: int
    http: HttpProbe | None = None
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class Canary:
    """One entry canary. ``type`` is ``http``, ``command`` or ``fixture``; the rest is the schema's shape as written."""

    type: str
    spec: dict[str, Any]

    @property
    def pass_rule(self) -> dict[str, Any]:
        return self.spec.get('pass') or {}


@dataclass(frozen=True)
class Route:
    path: str
    method: str
    stream: bool = False


@dataclass(frozen=True)
class FrontDoor:
    type: str
    port: int | None = None
    concurrency: int | None = None
    routes: tuple[Route, ...] = ()


@dataclass(frozen=True)
class Drain:
    type: str
    max_s: int = 0  # 0 for `kill`: removed at once


@dataclass(frozen=True)
class Manifest:
    name: str
    version: int
    runtime: str
    image: str
    placement: Placement
    run: Run
    network_egress: tuple[str, ...]
    artifacts: tuple[Artifact, ...]
    health: Health
    entry_canary: tuple[Canary, ...]
    profile: dict[str, Any]
    front_door: FrontDoor
    drain: Drain
    description: str = ''
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)  # the document as validated

    @property
    def image_digest(self) -> str:
        return image_digest(self.image)


def image_digest(image: str) -> str:
    """``sha256:<64 hex>`` from ``repo[:tag]@sha256:...``, or ''."""
    m = _DIGEST.search(image)
    return m.group(1) if m else ''


def gpu_type_of(card_name: str) -> str:
    """The manifest GPU type of a card as nvidia-smi names it."""
    if card_name in GPU_TYPE_NAMES:
        return GPU_TYPE_NAMES[card_name]
    words = [w for w in card_name.split() if w.lower() not in ('nvidia', 'geforce')]
    return re.sub(r'[^A-Za-z0-9]', '', ''.join(words)).upper()


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def schema_errors(document: Any) -> list[str]:
    validator = Draft202012Validator(load_schema())
    out = []
    for err in sorted(validator.iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path]):
        where = '.'.join(str(p) for p in err.absolute_path) or '<root>'
        out.append(f'{where}: {err.message}')
    return out


def consistency_errors(document: dict[str, Any], *, allow_placeholder_digest: bool = False) -> list[str]:
    """What the schema cannot say; the template's ``scripts/_manifest.py::consistency_errors``, in one place here."""
    problems: list[str] = []
    digest = image_digest(document.get('image', ''))
    if not digest:
        problems.append('image: not pinned by digest (must end in @sha256:<64 hex>)')
    elif digest == _ZERO_DIGEST and not allow_placeholder_digest:
        problems.append('image: the all-zeros digest is a placeholder; pin the digest your CI pushed')

    front_door = document.get('front_door', {})
    fd_type = front_door.get('type')
    routes = front_door.get('routes') or []
    seen: set[tuple[str, str]] = set()
    for route in routes:
        key = (route.get('method', ''), route.get('path', ''))
        if key in seen:
            problems.append(f'front_door.routes: duplicate route {key[0]} {key[1]}')
        seen.add(key)
    route_paths = {route.get('path') for route in routes}
    if fd_type == 'gateway-openai':
        problems += [
            f'front_door.routes: gateway-openai images must declare {need}'
            for need in _OPENAI_ROUTES
            if need not in route_paths
        ]

    health = document.get('health', {})
    if fd_type in _HTTP_FRONT_DOORS and 'http' in health and health['http'].get('port') != front_door.get('port'):
        problems.append(
            f'health.http.port ({health["http"].get("port")}) differs from front_door.port ({front_door.get("port")}); '
            'the controller probes the port it routes to'
        )

    for i, canary in enumerate(document.get('entry_canary') or []):
        if canary.get('type') != 'http':
            continue
        http = canary.get('http', {})
        if fd_type in _HTTP_FRONT_DOORS:
            if http.get('path') not in route_paths:
                problems.append(f'entry_canary[{i}].http.path {http.get("path")!r} is not one of front_door.routes')
            if http.get('port') != front_door.get('port'):
                problems.append(
                    f'entry_canary[{i}].http.port {http.get("port")} differs from front_door.port {front_door.get("port")}'
                )
        else:
            problems.append(f'entry_canary[{i}]: an http canary needs an HTTP front door (type {fd_type!r})')
        rule = canary.get('pass', {})
        if 'regex' in rule:
            try:
                re.compile(rule['regex'])
            except re.error as e:
                problems.append(f'entry_canary[{i}].pass.regex does not compile: {e}')

    mounts = [v.get('mount', '') for v in (document.get('run') or {}).get('volumes') or []]
    for i, artifact in enumerate(document.get('artifacts') or []):
        if mounts and not any(artifact.get('path', '').startswith(m.rstrip('/') + '/') for m in mounts):
            problems.append(
                f'artifacts[{i}].path {artifact.get("path")!r} is not under any run.volumes mount ({", ".join(mounts)}); '
                'the controller stages artifacts into the declared volume'
            )
    return problems


def parse_manifest(document: Any, *, allow_placeholder_digest: bool = False) -> Manifest:
    """Validate a parsed manifest (schema, then consistency) and type it. Raises ``ManifestError``."""
    if not isinstance(document, dict):
        raise ManifestError(['<root>: a manifest is a mapping'])
    problems = schema_errors(document)
    if not problems:
        problems = consistency_errors(document, allow_placeholder_digest=allow_placeholder_digest)
    if problems:
        raise ManifestError(problems)
    return _typed(document)


def load_manifest(path: str | Path, *, allow_placeholder_digest: bool = False) -> Manifest:
    try:
        document = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ManifestError([f'cannot load {path}: {e}']) from e
    return parse_manifest(document, allow_placeholder_digest=allow_placeholder_digest)


def _typed(d: dict[str, Any]) -> Manifest:
    p = d['placement']
    run = d.get('run') or {}
    health = d['health']
    front_door = d['front_door']
    drain = d['drain']
    http = health.get('http')
    return Manifest(
        name=d['name'],
        version=int(d['version']),
        runtime=d['runtime'],
        image=d['image'],
        description=d.get('description', ''),
        placement=Placement(
            GpuTypes(tuple(p['gpu_types'].get('include', ())), tuple(p['gpu_types'].get('exclude', ()))),
            int(p['cards_per_instance']),
            float(p['min_vram_gb']),
            int(p['max_load_s']),
        ),
        run=Run(
            {k: _env_value(v) for k, v in (run.get('env') or {}).items()},
            tuple(Volume(v['name'], v['mount'], bool(v.get('read_only', False))) for v in run.get('volumes') or []),
        ),
        network_egress=tuple((d.get('network') or {}).get('egress') or ()),
        artifacts=tuple(Artifact(a['path'], a['source'], a['revision'], a['sha256']) for a in d.get('artifacts') or []),
        health=Health(
            int(health['interval_s']),
            int(health['failure_threshold']),
            HttpProbe(http['path'], int(http['port']), int(http.get('expect_status', 200))) if http else None,
            tuple(health.get('command') or ()),
        ),
        entry_canary=tuple(Canary(c['type'], c) for c in d.get('entry_canary') or []),
        profile=dict(d.get('profile') or {}),
        front_door=FrontDoor(
            front_door['type'],
            front_door.get('port'),
            front_door.get('concurrency'),
            tuple(Route(r['path'], r['method'], bool(r.get('stream', False))) for r in front_door.get('routes') or []),
        ),
        drain=Drain(drain['type'], int(drain.get('max_s', 0))),
        raw=d,
    )


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)
