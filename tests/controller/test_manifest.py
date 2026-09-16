# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The manifest: the schema copy matches the template's, the 27B example parses into a typed Manifest, and every
schema and consistency problem is refused with its reason."""

import copy
from pathlib import Path

import pytest
import yaml

from gittensor.controller.manifest import (
    SCHEMA_PATH,
    ManifestError,
    gpu_type_of,
    load_manifest,
    parse_manifest,
)

FIXTURE = Path(__file__).parent / 'fixtures' / 'manifest_27b.yaml'
TEMPLATE_SCHEMA = Path('/home/kimbo/github-repos/project-gittensor/gittensor-compute-template/manifest.schema.json')


def doc():
    return yaml.safe_load(FIXTURE.read_text())


def problems(document, **kw):
    with pytest.raises(ManifestError) as e:
        parse_manifest(document, **kw)
    return ' | '.join(e.value.problems)


@pytest.mark.skipif(not TEMPLATE_SCHEMA.exists(), reason='the template repo is not checked out here')
def test_schema_is_byte_identical_to_the_template():
    assert SCHEMA_PATH.read_bytes() == TEMPLATE_SCHEMA.read_bytes()


def test_the_27b_example_parses_into_a_typed_manifest():
    m = load_manifest(FIXTURE)
    assert (m.name, m.version, m.runtime) == ('qwen3.8-27b-nvfp4', 1, 'sparkinfer')
    assert m.image_digest == 'sha256:' + '1' * 64
    assert m.placement.gpu_types.admits('RTX5090') and not m.placement.gpu_types.admits('H100')
    assert (m.placement.cards_per_instance, m.placement.min_vram_gb, m.placement.max_load_s) == (1, 30.0, 600)
    assert m.run.volumes[0].mount == '/models' and m.run.volumes[0].read_only and m.network_egress == ()
    assert m.artifacts[0].path == '/models/qwen3.8-27b-nvfp4'
    assert m.health.http is not None and m.health.http.port == 8080
    assert [c.type for c in m.entry_canary] == ['http', 'http'] and m.entry_canary[0].pass_rule['status'] == 200
    assert m.front_door.type == 'gateway-openai' and m.front_door.concurrency == 4 and len(m.front_door.routes) == 2
    assert (m.drain.type, m.drain.max_s) == ('requests', 60) and m.profile['decode_tps_single'] == 99
    assert m.raw['name'] == m.name


def test_a_placeholder_digest_is_refused_unless_allowed():
    d = doc()
    d['image'] = 'entrius/sparkinfer:19ef39ec2@sha256:' + '0' * 64
    assert 'placeholder' in problems(d)
    assert parse_manifest(d, allow_placeholder_digest=True).image_digest.endswith('0' * 64)


@pytest.mark.parametrize(
    'mutate, reason',
    [
        (lambda d: d.pop('drain'), "'drain' is a required property"),
        (lambda d: d.update(image='entrius/sparkinfer:19ef39ec2'), 'image'),
        (lambda d: d.update(owner='someone'), 'Additional properties'),
        (lambda d: d['placement'].update(cards_per_instance=0), 'placement.cards_per_instance'),
        (lambda d: d['health'].update(command=['true']), 'health'),  # http and command: oneOf
        (lambda d: d['front_door'].update(port=9090), 'health.http.port (8080) differs from front_door.port (9090)'),
        (lambda d: d['front_door']['routes'].append({'path': '/v1/models', 'method': 'GET'}), 'duplicate route'),
        (lambda d: d['front_door']['routes'].pop(), 'must declare /v1/models'),
        (lambda d: d['entry_canary'][0]['http'].update(path='/v1/score'), 'is not one of front_door.routes'),
        (lambda d: d['entry_canary'][0]['pass'].update(regex='('), 'does not compile'),
        (lambda d: d['artifacts'][0].update(path='/weights/x'), 'not under any run.volumes mount'),
    ],
)
def test_schema_and_consistency_problems_are_refused(mutate, reason):
    d = copy.deepcopy(doc())
    mutate(d)
    assert reason in problems(d)


def test_not_a_mapping_and_unreadable_files():
    assert 'mapping' in problems(['not', 'a', 'mapping'])
    with pytest.raises(ManifestError, match='cannot load'):
        load_manifest('/nonexistent/manifest.yaml')


def test_gpu_type_of_card_names():
    assert gpu_type_of('NVIDIA GeForce RTX 5090') == 'RTX5090'
    assert gpu_type_of('NVIDIA GeForce RTX 4090') == 'RTX4090'
    assert gpu_type_of('NVIDIA H100 80GB HBM') == 'H10080GBHBM'
