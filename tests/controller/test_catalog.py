# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The GPU catalog: the types it lists, the VRAM window of each, and how the full check's spec rule and ``gitt up``
use it. A listed type is known but not admitted; the spec is the catalog's, picked by the name the box reports."""

import pytest

from gittensor.cli.up_commands.prereqs import check_gpu_model
from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import why as w
from gittensor.controller.checks.catalog import (
    LISTED,
    QUALIFIED,
    CatalogError,
    load_catalog,
    parse_catalog,
    spec_for_name,
    spec_for_type,
)
from gittensor.controller.checks.config import RTX_5090
from gittensor.controller.checks.scrape import parse_nvidia_smi
from gittensor.controller.manifest import gpu_type_of
from tests.controller.conftest import fixture

H100 = 'GPU-11111111-2222-4333-8444-555555555555, NVIDIA H100 80GB HBM3, 580.65.06, 81559, 700.00, 700.00, 700.00, 00000000:01:00.0, 9.0'


def card(line: str, **changes: str):
    cols = [c.strip() for c in line.split(',')]
    names = ('uuid', 'name', 'driver', 'vram', 'limit', 'default', 'max', 'bus', 'cap')
    cols = [changes.get(n, c) for n, c in zip(names, cols)]
    return parse_nvidia_smi(', '.join(cols))


def test_the_catalog_lists_every_phase_1_type_and_only_the_5090_is_qualified():
    catalog = load_catalog()
    assert set(catalog) == {'RTX5090', 'RTXPRO6000', 'L40S', 'H100', 'H200', 'B200', 'B300'}
    assert [t for t, s in catalog.items() if s.qualified] == ['RTX5090']
    assert all(s.status in (QUALIFIED, LISTED) for s in catalog.values())
    assert catalog['RTX5090'] is RTX_5090 and (RTX_5090.vram_total_mib_min, RTX_5090.vram_total_mib_max) == (
        32_000,
        33_000,
    )


@pytest.mark.parametrize(
    'gpu_type, observed_mib',
    # What real cards report (Lium's gpu_spec_table.py, 10/5): every one inside its own type's window.
    [('RTXPRO6000', 97887), ('L40S', 46068), ('L40S', 49140), ('H100', 81559), ('H200', 143771), ('B200', 183359)]
    + [('B300', 275040)],
)
def test_an_observed_card_is_inside_its_types_window_and_no_other(gpu_type, observed_mib):
    inside = [t for t, s in load_catalog().items() if s.vram_total_mib_min <= observed_mib <= s.vram_total_mib_max]
    assert inside == [gpu_type]


def test_names_map_to_types_and_an_unknown_name_normalises():
    assert spec_for_name(' NVIDIA H100 PCIe ') is spec_for_type('H100')
    assert gpu_type_of('NVIDIA H200 NVL') == 'H200' and gpu_type_of('NVIDIA B300 SXM6 PC') == 'B300'
    assert gpu_type_of('NVIDIA RTX PRO 6000 Blackwell Server Edition') == 'RTXPRO6000'
    assert spec_for_name('NVIDIA GeForce RTX 4090') is None and gpu_type_of('NVIDIA GeForce RTX 4090') == 'RTX4090'


def test_a_listed_type_is_not_admitted_until_it_is_qualified():
    result = ck.check_gpu_spec(card(H100))
    assert not result.passed and 'listed but not qualified' in result.evidence['reason']
    assert result.evidence[w.PUBLIC] == {'code': w.SPEC_MODEL, 'n': 1}


def test_a_box_is_held_to_our_numbers_for_the_type_it_names():
    five = fixture('nvidia_smi_5090.csv')
    assert ck.check_gpu_spec(card(five)).evidence['gpu_type'] == 'RTX5090'
    small = ck.check_gpu_spec(card(five, vram='24564'))  # a 24 GB card under a 5090's name
    assert not small.passed and small.evidence[w.PUBLIC]['code'] == w.SPEC_VRAM
    # An explicit spec (a type once qualified) judges the same way: a 96 GB card is no H100, whatever it is called.
    h100 = spec_for_type('H100')
    assert ck.check_gpu_spec(card(H100), h100).passed
    big = ck.check_gpu_spec(card(H100, vram='97887'), h100)
    assert not big.passed and big.evidence[w.PUBLIC]['code'] == w.SPEC_VRAM
    wrong_arch = ck.check_gpu_spec(card(H100, cap='12.0'), h100)
    assert not wrong_arch.passed and wrong_arch.evidence[w.PUBLIC]['code'] == w.SPEC_COMPUTE_CAP


def test_every_card_on_a_box_is_one_type():
    mixed = card(fixture('nvidia_smi_5090.csv')) + card(H100)
    result = ck.check_gpu_spec(mixed)
    assert not result.passed and result.evidence[w.PUBLIC] == {'code': w.SPEC_MODEL, 'n': 1}


def test_a_bad_catalog_is_refused():
    row = {'names': ['X'], 'compute_cap': '9.0', 'vram_mib': 1000, 'status': 'qualified'}
    assert parse_catalog({'_comment': 'skipped', 'A': row})['A'].vram_total_mib_min == 900
    with pytest.raises(CatalogError, match='both A and B'):
        parse_catalog({'A': row, 'B': row})
    with pytest.raises(CatalogError, match='status'):
        parse_catalog({'A': {**row, 'status': 'blessed'}})
    with pytest.raises(CatalogError, match='vram_mib'):
        parse_catalog({'A': {k: v for k, v in row.items() if k != 'vram_mib'}})


def test_gitt_up_names_the_model_rule_before_the_controller_does():
    five = 'NVIDIA GeForce RTX 5090'
    assert check_gpu_model([five, five]) is None
    listed = check_gpu_model(['NVIDIA H100 PCIe'])
    assert listed is not None and 'pool admits RTX5090 only' in listed.detail and not listed.required
    unknown = check_gpu_model([five, 'NVIDIA GeForce RTX 4090'])
    assert unknown is not None and 'RTX 4090' in unknown.detail
