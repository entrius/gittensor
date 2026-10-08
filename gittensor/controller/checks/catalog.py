# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The GPU catalog (``gpu_catalog.json``): every card type the pool knows and what a card of it must look like.

One table for the full check's spec, the manifest GPU type of a card name, placement's spec VRAM and ``gitt up``'s
model check. A type is ``qualified`` (admitted: the proof has been measured on a real card of it) or ``listed``
(known, not admitted yet). The spec is always ours, picked by the name the box reports and then held against the
box: a card is never judged by its own numbers.

``counts`` is the box sizes the pool admits for the type (vault ``29`` §1 #3): a rental takes the whole box, so a box
is a unit we sell, and we sell the sizes the market has (Lium offers 1x, 2x, 4x and 8x). A box of five cards is not
admitted at all, rentable or not.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from gittensor.controller.checks.vendor import NVIDIA, VENDORS

CATALOG_PATH = Path(__file__).with_name('gpu_catalog.json')
QUALIFIED = 'qualified'
LISTED = 'listed'
# The accepted window around a type's nominal size (Lium's gpu_spec_table.py): NVML's total is the physical memory
# less the vendor's and the driver's reservations, seen up to ~7% under nominal (an L40S reports 46068 MiB of 49152),
# and slightly over on some cards. One window per type, so a size between two types is no type at all.
VRAM_FLOOR_RATIO = 0.90
VRAM_CEIL_RATIO = 1.05
COUNTS_DEFAULT = (1, 2, 4, 8)  # the box sizes admitted when a type's row names none
COUNT_MAX = 8


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CardSpec:
    """What every card on an admitted box must look like in ``nvidia-smi --query-gpu``."""

    gpu_type: str = 'RTX5090'  # the manifest `placement.gpu_types` name and the pay table's row
    names: Tuple[str, ...] = ('NVIDIA GeForce RTX 5090',)
    compute_cap: str = '12.0'  # sm_120, what the proof kernel (docker/proof/kernel) is compiled for
    vram_total_mib_min: int = 32_000  # a 5090 reports 32607 MiB
    vram_total_mib_max: int = 33_000
    counts: Tuple[int, ...] = COUNTS_DEFAULT  # the card counts a box of this type may carry, ascending
    status: str = QUALIFIED
    # The vendor switch (vault 30 §1 #2): ``nvidia`` rows are judged by ``names`` + ``compute_cap`` as today; an ``amd``
    # row is judged by its PCI device ids and gfx target (30 §2), ``names`` being the display names only.
    vendor: str = NVIDIA
    pci_ids: Tuple[str, ...] = ()  # e.g. ('0x74a1',), lowercase hex as sysfs prints it
    gfx_target: str = ''  # e.g. 'gfx942', the proof binary's build target

    @property
    def name(self) -> str:
        return self.names[0]

    @property
    def qualified(self) -> bool:
        return self.status == QUALIFIED


def parse_catalog(doc: dict, source: str = '') -> Dict[str, CardSpec]:
    specs: Dict[str, CardSpec] = {}
    seen: Dict[str, str] = {}
    for gpu_type, row in doc.items():
        if gpu_type.startswith('_'):
            continue
        try:
            names = tuple(str(n) for n in row['names'])
            nominal = int(row['vram_mib'])
            spec = CardSpec(
                gpu_type=gpu_type,
                names=names,
                # an AMD row's compute capability is its gfx target (what the KFD topology reports for the card)
                compute_cap=str(row.get('compute_cap') or row.get('gfx_target') or ''),
                vram_total_mib_min=int(row.get('vram_mib_min', nominal * VRAM_FLOOR_RATIO)),
                vram_total_mib_max=int(row.get('vram_mib_max', nominal * VRAM_CEIL_RATIO)),
                counts=tuple(sorted({int(c) for c in row.get('counts', COUNTS_DEFAULT)})),
                status=str(row['status']),
                vendor=str(row.get('vendor', NVIDIA)).strip().lower(),
                pci_ids=tuple(str(p).strip().lower() for p in row.get('pci_ids', ())),
                gfx_target=str(row.get('gfx_target', '')).strip().lower(),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise CatalogError(f'{source}: {gpu_type}: {e!r}') from e
        if not names or spec.status not in (QUALIFIED, LISTED):
            raise CatalogError(f'{source}: {gpu_type}: needs at least one name and a status of qualified or listed')
        if not 0 < spec.vram_total_mib_min <= spec.vram_total_mib_max:
            raise CatalogError(f'{source}: {gpu_type}: empty VRAM window')
        if not spec.counts or not all(1 <= c <= COUNT_MAX for c in spec.counts):
            raise CatalogError(f'{source}: {gpu_type}: counts must be one or more box sizes between 1 and {COUNT_MAX}')
        if spec.vendor not in VENDORS:
            raise CatalogError(f'{source}: {gpu_type}: vendor must be one of {", ".join(VENDORS)}')
        if spec.vendor != NVIDIA and not (spec.pci_ids and spec.gfx_target):
            raise CatalogError(f'{source}: {gpu_type}: a {spec.vendor} row needs pci_ids and gfx_target')
        for name in names:
            if name in seen:
                raise CatalogError(f'{source}: {name!r} is listed under both {seen[name]} and {gpu_type}')
            seen[name] = gpu_type
        specs[gpu_type] = spec
    return specs


@lru_cache(maxsize=1)
def load_catalog() -> Dict[str, CardSpec]:
    return parse_catalog(json.loads(CATALOG_PATH.read_text()), str(CATALOG_PATH))


def spec_for_name(card_name: str) -> Optional[CardSpec]:
    """The catalog entry for a card as nvidia-smi names it, qualified or not; None for a card the catalog does not
    know."""
    name = card_name.strip()
    return next((spec for spec in load_catalog().values() if name in spec.names), None)


def spec_for_type(gpu_type: str) -> Optional[CardSpec]:
    return load_catalog().get(gpu_type)


def spec_for_pci_id(device_id: str) -> Optional[CardSpec]:
    """The AMD catalog entry for a PCI device id as sysfs prints it (``0x74a1``), qualified or not; None for an id
    no row lists. An AMD card is matched on its ids, never on a marketing name (30 §4)."""
    wanted = device_id.strip().lower()
    return next((spec for spec in load_catalog().values() if wanted and wanted in spec.pci_ids), None)
