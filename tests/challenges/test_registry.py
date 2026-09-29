# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The registry refuses what it cannot pay or run: shares over one pool (summed exactly) and a mismatched package."""

import json

import pytest

from gittensor.challenges.registry import REGISTRY_PATH, RegistryError, import_challenge, load_registry


def write(tmp_path, entries: dict):
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps(entries))
    return path


def test_shares_are_summed_exactly(tmp_path, registry_path):
    entry = json.loads(registry_path.read_text())['fake-echo']
    thirds = {c: {**entry, 'emission_share': s} for c, s in (('a', 0.1), ('b', 0.2), ('c', 0.7))}
    assert set(load_registry(write(tmp_path, thirds))) == {'a', 'b', 'c'}

    with pytest.raises(RegistryError, match='sum to 1.1'):
        load_registry(write(tmp_path, {'a': entry, 'b': {**entry, 'emission_share': 0.6}}))


def test_a_package_that_is_not_the_registered_version_is_refused(tmp_path, registry_path):
    entry = json.loads(registry_path.read_text())['fake-echo']
    registry = load_registry(write(tmp_path, {'fake-echo': {**entry, 'version': '0.2.0'}}))

    with pytest.raises(RegistryError, match='the registry wants'):
        import_challenge(registry['fake-echo'])


def test_the_shipped_registry_loads_and_pays_nothing_yet():
    assert all(c.emission_share == 0 for c in load_registry(REGISTRY_PATH).values())
