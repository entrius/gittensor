# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The image registry: what blessing produces, and the operator's deployment settings (vault ``25`` "Blessing, images
and registry", ``23`` §6, §8, ``26`` §3-4).

A **registry entry** is ``{name, version, image, manifest, blessed_at}``: the digest-pinned image and its
author-owned manifest, verbatim, signed together by the release key (an OpenSSH signature over the entry's canonical
JSON bytes, namespace ``gt-registry``). Entries live in ``<state-dir>/registry/<name>@<version>.json`` + ``.sig``.
An entry blessed from an author's published image also carries ``source_image``: the author's own reference, kept as
provenance, while ``image`` is our digest-preserving copy under ``entrius/`` (the one boxes pull, so an author
deleting or retagging their package breaks nothing). Same digest or the entry is refused.
An entry may also carry ``qualified``: **our** measurements from qualifying the image on our own card (``25``
"Blessing"), signed with the rest but kept beside the author's manifest, never inside it. It is optional (an entry
without it has exactly the bytes it always had) and nothing reads it yet.
The controller **re-verifies the signature every time it reads an entry** and never runs one that does not verify:
the files (later the database) are a request, not trusted input.

**Deployment settings** are separate and operator-owned: ``<state-dir>/deployments.json`` =
``{entry_id: {enabled, replicas, box}}``. Enabling an entry never changes what was signed.
"""

from __future__ import annotations

import copy
import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gittensor.agent.channel import ChannelError, verify
from gittensor.agent.config import DEV_KEY_MARKER, RELEASE_PUBKEY_OPENSSH
from gittensor.controller.manifest import Manifest, ManifestError, parse_manifest

REGISTRY_NAMESPACE = 'gt-registry'  # `ssh-keygen -Y sign -n`; a channel signature does not verify as an entry
ENTRY_FIELDS = ('blessed_at', 'image', 'manifest', 'name', 'version')
QUALIFIED = 'qualified'  # optional: our measurements
SOURCE_IMAGE = 'source_image'  # optional: the author's reference `image` was copied from, same digest
OPTIONAL_FIELDS = (QUALIFIED, SOURCE_IMAGE)
_DIGEST = re.compile(r'@(sha256:[0-9a-f]{64})$')
_ENTRY_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{1,63}@[1-9][0-9]*$')
# The qualification block: field -> type. `box` is the hotkey of the box it was measured on, or "local".
QUALIFIED_REQUIRED = {
    'at': 'number',
    'box': 'string',
    'driver': 'string',
    'load_s': 'number',
    'health_ok': 'boolean',
    'canary_ok': 'boolean',
    'vram_gb': 'number',
    'notes': 'string',
}
QUALIFIED_OPTIONAL = {'decode_tps_single': 'number', 'prefill_tps': 'number'}


class RegistryError(Exception):
    """An entry is missing, malformed, or does not verify. Nothing is run from it."""


def canonical_json(obj: Any) -> bytes:
    """The bytes a registry signature covers: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _of_type(value: Any, kind: str) -> bool:
    if kind == 'boolean':
        return isinstance(value, bool)
    if kind == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return isinstance(value, str)


def parse_qualified(block: Any) -> dict[str, Any]:
    """The ``qualified`` block, checked: every required field with its type, the optional ones typed when present, and
    nothing else. Raises ``RegistryError`` naming every problem."""
    if not isinstance(block, dict):
        raise RegistryError(f'{QUALIFIED}: expected an object')
    fields = {**QUALIFIED_REQUIRED, **QUALIFIED_OPTIONAL}
    problems = [f'{key} missing' for key in QUALIFIED_REQUIRED if key not in block]
    problems += [
        f'{key} must be a {kind}' for key, kind in fields.items() if key in block and not _of_type(block[key], kind)
    ]
    unknown = sorted(set(block) - set(fields))
    if unknown:
        problems.append('unknown field(s): ' + ', '.join(map(str, unknown)))
    if isinstance(block.get('box'), str) and not block['box'].strip():
        problems.append('box must be a hotkey or "local"')
    if problems:
        raise RegistryError(f'{QUALIFIED}: ' + '; '.join(problems))
    return copy.deepcopy(block)


def parse_source_image(source_image: Any, image: str) -> str:
    """The ``source_image`` field, checked: digest-pinned, and the same digest as ``image`` (a copy, not a rebuild)."""
    if not isinstance(source_image, str) or not (source := _DIGEST.search(source_image)):
        raise RegistryError(f'{SOURCE_IMAGE}: expected repo[:tag]@sha256:<64 hex>')
    pinned = _DIGEST.search(image)
    if pinned is None or pinned.group(1) != source.group(1):
        raise RegistryError(f'{SOURCE_IMAGE}: its digest is not the digest of image ({image})')
    return source_image


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    version: int
    image: str
    manifest: dict[str, Any]
    blessed_at: int
    qualified: dict[str, Any] | None = None  # our measurements, beside the manifest; None on entries blessed without
    source_image: str | None = None  # the author's reference `image` was copied from; None when `image` is the source

    @property
    def entry_id(self) -> str:
        return f'{self.name}@{self.version}'

    def as_dict(self) -> dict[str, Any]:
        out = {
            'name': self.name,
            'version': self.version,
            'image': self.image,
            'manifest': self.manifest,
            'blessed_at': self.blessed_at,
        }
        if self.qualified is not None:
            out[QUALIFIED] = self.qualified
        if self.source_image is not None:
            out[SOURCE_IMAGE] = self.source_image
        return out

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.as_dict())


@dataclass(frozen=True)
class VerifiedEntry:
    """An entry whose signature verified on this read, with its manifest validated and typed."""

    entry: RegistryEntry
    manifest: Manifest

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id


def make_entry(
    document: dict[str, Any],
    image: str | None = None,
    now: float | None = None,
    qualified: dict[str, Any] | None = None,
    source_image: str | None = None,
) -> VerifiedEntry:
    """An unsigned entry from a manifest document, with ``image`` (``repo@sha256:...``) pinned over the manifest's own
    and, optionally, our ``qualified`` measurements beside it. The manifest must pass the schema and the consistency
    checks, placeholder digest refused; a malformed ``qualified`` block, or a ``source_image`` whose digest is not
    the image's, raises ``RegistryError``."""
    doc = copy.deepcopy(document)
    if image:
        doc['image'] = image
    manifest = parse_manifest(doc)
    block = parse_qualified(qualified) if qualified is not None else None
    source = parse_source_image(source_image, manifest.image) if source_image is not None else None
    entry = RegistryEntry(
        manifest.name, manifest.version, manifest.image, doc, int(time.time() if now is None else now), block, source
    )
    return VerifiedEntry(entry, manifest)


def sign_bytes(payload: bytes, key_path: str | Path, namespace: str = REGISTRY_NAMESPACE, run=subprocess.run) -> bytes:
    """An OpenSSH signature over ``payload`` with the private key at ``key_path`` (``ssh-keygen -Y sign`` on stdin)."""
    try:
        proc = run(
            ['ssh-keygen', '-Y', 'sign', '-f', str(key_path), '-n', namespace],
            input=payload,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RegistryError(f'ssh-keygen unavailable: {e}') from e
    if proc.returncode != 0 or b'BEGIN SSH SIGNATURE' not in proc.stdout:
        raise RegistryError(f'signing with {key_path} failed: {proc.stderr.decode(errors="replace").strip()[:300]}')
    return proc.stdout


def load_release_pubkey(path: str | Path | None, allow_dev_keys: bool = False) -> str:
    """The public key entries must verify against: the compiled release key, or (dev) the one in ``path``. A key
    tagged DO-NOT-SHIP is refused without ``allow_dev_keys``."""
    if path is None:
        return RELEASE_PUBKEY_OPENSSH
    try:
        key = Path(path).expanduser().read_text().strip().splitlines()[0]
    except (OSError, IndexError) as e:
        raise RegistryError(f'release public key {path}: {e}') from e
    if DEV_KEY_MARKER in key and not allow_dev_keys:
        raise RegistryError(f'{path} is a dev key ({DEV_KEY_MARKER}); pass --allow-dev-keys to trust it')
    return key


class Registry:
    """The entry directory, read through signature verification."""

    def __init__(self, root: str | Path, pubkey: str = RELEASE_PUBKEY_OPENSSH, run=subprocess.run):
        self.root, self.pubkey, self._run = Path(root), pubkey, run

    def paths(self, entry_id: str) -> tuple[Path, Path]:
        if not _ENTRY_ID.match(entry_id):
            raise RegistryError(f'{entry_id!r} is not an entry id (<name>@<version>)')
        payload = self.root / f'{entry_id}.json'
        return payload, payload.with_name(payload.name + '.sig')

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name[: -len('.json')] for p in self.root.glob('*.json') if _ENTRY_ID.match(p.stem))

    def write(self, verified: VerifiedEntry, signature: bytes) -> Path:
        """Store a signed entry. The signature is checked against the trusted key first: the registry never holds an
        entry the controller would refuse."""
        payload = verified.entry.canonical_bytes()
        self._verify(verified.entry_id, payload, signature)
        path, sig_path = self.paths(verified.entry_id)
        self.root.mkdir(parents=True, exist_ok=True)
        for target, data in ((path, payload), (sig_path, signature)):
            tmp = target.with_name(target.name + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(target)
        return path

    def read(self, entry_id: str) -> VerifiedEntry:
        """Verify, then parse. Raises ``RegistryError`` on anything short of a valid, canonical, signed entry whose
        manifest passes the schema and names the same image, name and version."""
        path, sig_path = self.paths(entry_id)
        try:
            payload, signature = path.read_bytes(), sig_path.read_bytes()
        except OSError as e:
            raise RegistryError(f'{entry_id}: {e}') from e
        self._verify(entry_id, payload, signature)
        try:
            doc = json.loads(payload)
        except ValueError as e:
            raise RegistryError(f'{entry_id}: not JSON: {e}') from e
        if not isinstance(doc, dict) or sorted(set(doc) - set(OPTIONAL_FIELDS)) != list(ENTRY_FIELDS):
            raise RegistryError(
                f'{entry_id}: an entry has exactly the fields {", ".join(ENTRY_FIELDS)}, and optionally {", ".join(OPTIONAL_FIELDS)}'
            )
        if canonical_json(doc) != payload:
            raise RegistryError(f'{entry_id}: not in canonical form')
        try:
            entry = RegistryEntry(
                str(doc['name']),
                int(doc['version']),
                str(doc['image']),
                dict(doc['manifest']),
                int(doc['blessed_at']),
                parse_qualified(doc[QUALIFIED]) if QUALIFIED in doc else None,
                parse_source_image(doc[SOURCE_IMAGE], str(doc['image'])) if SOURCE_IMAGE in doc else None,
            )
            manifest = parse_manifest(entry.manifest)
        except (TypeError, ValueError, ManifestError, RegistryError) as e:
            raise RegistryError(f'{entry_id}: {e}') from e
        if entry.entry_id != entry_id or (manifest.name, manifest.version, manifest.image) != (
            entry.name,
            entry.version,
            entry.image,
        ):
            raise RegistryError(f'{entry_id}: the entry, its file name and its manifest disagree')
        return VerifiedEntry(entry, manifest)

    def _verify(self, entry_id: str, payload: bytes, signature: bytes) -> None:
        try:
            verify(payload, signature, self.pubkey, run=self._run, namespace=REGISTRY_NAMESPACE)
        except ChannelError as e:
            raise RegistryError(f'{entry_id}: {e}') from e


# ---------------------------------------------------------------- deployments ---------------------------------------


@dataclass
class Deployment:
    enabled: bool = False
    replicas: int = 0
    box: str = ''  # pin: place only on this box (hotkey); '' = any box. For canary runs on our own cards (Kimbo 9/15).

    @property
    def desired(self) -> int:
        return self.replicas if self.enabled else 0


class DeploymentStore:
    """``deployments.json``: ``{entry_id: {enabled, replicas}}``, operator-owned (the admin page, later)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.deployments: dict[str, Deployment] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text() or '{}')
            self.deployments = {
                k: Deployment(bool(v.get('enabled', False)), max(0, int(v.get('replicas', 0))), str(v.get('box') or ''))
                for k, v in raw.items()
            }

    def get(self, entry_id: str) -> Deployment:
        return self.deployments.get(entry_id) or Deployment()

    def set(
        self, entry_id: str, enabled: bool | None = None, replicas: int | None = None, box: str | None = None
    ) -> Deployment:
        current = self.get(entry_id)
        new = Deployment(
            current.enabled if enabled is None else enabled,
            current.replicas if replicas is None else replicas,
            current.box if box is None else box,
        )
        if new.replicas < 0:
            raise ValueError('replicas must be >= 0')
        self.deployments[entry_id] = new
        self.save()
        return new

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps({k: vars(v) for k, v in sorted(self.deployments.items())}, indent=1))
        tmp.replace(self.path)
