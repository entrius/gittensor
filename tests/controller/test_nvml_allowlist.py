# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The NVML allowlist fails closed: empty driver, unknown driver, missing digest, mismatch, kernel disagreement."""

import json

from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from tests.controller.conftest import DRIVER, FIXTURES, NVML_MD5


def test_known_driver_and_digest_pass():
    al = NvmlAllowlist.from_file(FIXTURES / 'nvml_allowlist.json')
    r = al.judge(DRIVER, NVML_MD5, kernel_driver=DRIVER)
    assert r.passed and r.name == 'nvml_digest' and r.evidence['driver'] == DRIVER
    assert al.judge(DRIVER, NVML_MD5.upper()).passed  # case-insensitive md5


def test_unknown_driver_fails_closed_and_is_named():
    al = NvmlAllowlist.from_file(FIXTURES / 'nvml_allowlist.json')
    r = al.judge('999.99.99', NVML_MD5)
    assert not r.passed and 'unknown driver' in r.evidence['reason'] and r.evidence['driver'] == '999.99.99'


def test_empty_driver_string_fails_even_with_a_listed_digest():
    """Lium's `checks/nvml_digest.py:29` skips the whole check when the driver string is empty; ours fails."""
    al = NvmlAllowlist({'': [NVML_MD5], DRIVER: [NVML_MD5]})
    assert '' not in al.by_driver
    r = al.judge('', NVML_MD5)
    assert not r.passed and r.evidence['reason'] == 'empty driver string'
    assert not al.judge(None, NVML_MD5).passed


def test_mismatch_missing_digest_and_kernel_disagreement_fail():
    al = NvmlAllowlist({DRIVER: [NVML_MD5]})
    r = al.judge(DRIVER, 'f' * 32)
    assert not r.passed and r.evidence['reason'] == 'digest mismatch' and r.evidence['expected'] == [NVML_MD5]
    assert 'not found' in al.judge(DRIVER, '').evidence['reason']
    r = al.judge(DRIVER, NVML_MD5, kernel_driver='575.64.03')
    assert not r.passed and 'kernel module' in r.evidence['reason']


def test_load_from_json_text_and_empty_location():
    al = NvmlAllowlist.from_json(json.dumps({DRIVER: NVML_MD5}))  # a bare string is one digest
    assert al.by_driver == {DRIVER: {NVML_MD5}}
    empty = NvmlAllowlist.load('')
    assert empty.drivers == set() and not empty.judge(DRIVER, NVML_MD5).passed
