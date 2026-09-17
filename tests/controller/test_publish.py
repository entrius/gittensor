# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``public/fleet.json``: the contract's shape, that nothing private on a box or an instance record reaches it (address,
ports, container / image ids, raw GPU UUIDs, host keys, error text), the atomic write, and the running controller
writing it on the scorecard tick and on its own interval."""

import json
import os
import stat
from typing import Any, cast

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli: circular import)
from gittensor.controller import cli as ctl
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import (
    ADMIT,
    BENCHED,
    DRAINING,
    IDLE,
    LEASED,
    BoxState,
    CardState,
    StateStore,
)
from gittensor.controller.daemon import Controller, Intervals
from gittensor.controller.pay.oracle import FailSafeOracle, StaticOracle
from gittensor.controller.publish import (
    SCHEMA,
    Publisher,
    build_fleet,
    card_hash,
    fleet_path,
    public_image,
    write_fleet,
)
from gittensor.controller.reconcile import InstanceRecord, InstanceStore
from gittensor.controller.registry import Registry
from gittensor.controller.standing import CHECK_FAILED, CLEAN_LEASE
from tests.controller.test_cli import invoke

UUID_A = 'GPU-4f2a6b8c-1d3e-4a5b-9c7d-0e1f2a3b4c5d'
UUID_B = 'GPU-9b8c7d6e-5f40-4132-a2b3-c4d5e6f70819'
UUID_C = 'GPU-0a1b2c3d-4e5f-4061-8273-8495a6b7c8d9'
HK_A = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
HK_B = '5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty'
HK_C = '5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y'
NOW = 1_789_000_000.0
IMAGE = 'entrius/qwen3.8-27b-nvfp4:6@sha256:' + 'd5' * 32

# Every value here is private: none may appear anywhere in the published document.
PRIVATE = {
    'host': '203.0.113.77',
    'ssh_port': 50030,
    'host_key': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPrivateHostKeyMaterial',
    'mapped_port': 50085,
    'host_port': 20417,
    'container_id': 'c0ffee' * 10 + 'beef',
    'image_id': 'sha256:' + '7d' * 32,
    'nvml_md5': '9e107d9d372bb6826bd81d3542a419d6',
    'transport': 'SshTransportError: 203.0.113.77:50030: root@203.0.113.77: Permission denied (publickey).',
    'moved_host': '198.51.100.9',
    'reason': 'operator note: pod at 203.0.113.77 re-rented',
    'path': '/home/isaiah/.gittensor/controller',
}


def fleet(now: float = NOW) -> tuple[dict[str, BoxState], dict[str, InstanceRecord]]:
    """A leased + draining box with every private field set, a benched box, and a box admitted but never proved."""
    boxes = {
        HK_A: BoxState(
            HK_A, status=IDLE, pinned_uuids=[UUID_A, UUID_B], card_name='NVIDIA GeForce RTX 5090',
            last_check_at=now - 300, host=PRIVATE['host'], port=PRIVATE['ssh_port'], host_key=PRIVATE['host_key'],
            port_map={'8080': PRIVATE['mapped_port']}, workload_ports=[PRIVATE['host_port'], PRIVATE['host_port'] + 9],
            source='chain', identity={'nvml_md5': PRIVATE['nvml_md5'], 'power_limits': {UUID_A: 575}},
            endpoint_changed={'host': PRIVATE['moved_host'], 'port': 2222, 'host_key': PRIVATE['host_key'], 'at': now},
            cards={UUID_A: CardState(LEASED, 'i-1', now - 2_400), UUID_B: CardState(DRAINING, 'i-2', now - 30)},
            last_failed=['gpu_uuid_pin', PRIVATE['transport']],
            standing_events=[
                {'at': now - 9_000, 'kind': 'released', 'reason': PRIVATE['reason']},
                {'at': now - 5_000, 'kind': CLEAN_LEASE, 'instance': 'i-0', 'uuid': UUID_A, 'leased_s': 2_785.4},
            ],
        ),
        HK_B: BoxState(
            HK_B, status=BENCHED, card_name='NVIDIA GeForce RTX 5090', host='203.0.113.78', port=2200,
            last_check_at=now - 1_500, bench_until=now + 3_600, benched_at=now - 1_500, last_failed=['gpu_uuid_pin'],
            standing_events=[{'at': now - 1_500, 'kind': CHECK_FAILED, 'failed': ['gpu_uuid_pin']}],
        ),
        HK_C: BoxState(HK_C, status=ADMIT, host='203.0.113.79', port=2200, source='chain'),
    }  # fmt: skip
    heartbeat = {'at': now - 20, 'ok': None, 'error': PRIVATE['transport']}
    instances = {
        'i-1': InstanceRecord(
            'i-1', 'qwen3.8-27b-nvfp4@6', HK_A, UUID_A, container_id=PRIVATE['container_id'], host=PRIVATE['host'],
            port=PRIVATE['mapped_port'], host_port=PRIVATE['host_port'], healthy=True, started_at=now - 2_430,
            leased_at=now - 2_400, image_id=PRIVATE['image_id'], last_heartbeat_at=now - 80, heartbeat_ok=None,
            heartbeat=heartbeat, heartbeat_misses=1, health_ok=True, health_detail='GET 203.0.113.77:8080 -> 200',
        ),
        'i-2': InstanceRecord(
            'i-2', 'qwen3.8-27b-nvfp4@6', HK_A, UUID_B, container_id=PRIVATE['container_id'][::-1],
            host=PRIVATE['host'], port=PRIVATE['mapped_port'] + 1, host_port=PRIVATE['host_port'] + 1, healthy=False,
            draining=True, leased_at=now - 4_000, image_id=PRIVATE['image_id'], last_heartbeat_at=now - 40,
            heartbeat_ok=True,
        ),
    }  # fmt: skip
    return boxes, instances


def build(tmp_path, now: float = NOW, **kw) -> dict:
    boxes, instances = fleet(now)
    status = {
        'pid': 4242,
        'intervals': {'round_s': 1200.0},
        'round': {'n': 10, 'finished_at': now - 500, 'provider': 'gtp-dev-e9d2b82e', 'boxes': {}},
        'reconcile': {'unreachable': {HK_A: PRIVATE['transport']}},
        'pay': {'path': PRIVATE['path'] + '/scorecard/latest.json'},
    }
    return build_fleet(tmp_path, boxes, instances, status, True, now, lambda entry: IMAGE, 'finney', 74, **kw)


def test_the_contract(tmp_path):
    doc = build(tmp_path)
    assert doc['schema'] == SCHEMA == 1 and doc['generated_at'] == NOW
    assert (doc['network'], doc['netuid']) == ('finney', 74)
    assert doc['controller'] == {
        'running': True,
        'round_n': 10,
        'last_round_at': NOW - 500,
        'round_interval_s': 1200.0,
        'publish_interval_s': cfg.PUBLISH_INTERVAL_S,
    }
    assert doc['scorecard'] is None and doc['oracle'] is None  # no scorecard written yet
    assert doc['rates']['RTX5090'] == {
        'idle_usd_per_card_hour': 0.35,
        'leased_usd_per_card_hour': 1.0,
        'source': 'table',
    }
    assert doc['totals'] == {'boxes': 3, 'cards': 2, 'cards_by_state': {LEASED: 1, DRAINING: 1}}
    a, b, c = (next(x for x in doc['boxes'] if x['hotkey'] == hk) for hk in (HK_A, HK_B, HK_C))
    assert set(a) == {
        'hotkey', 'uid', 'status', 'standing', 'gpu_type', 'card_count', 'last_check_at', 'last_failed',
        'bench_until', 'benched_reason', 'pay', 'last_event', 'cards',
    }  # fmt: skip
    assert (a['status'], a['standing'], a['gpu_type'], a['card_count'], a['uid']) == (
        IDLE, 'probation', 'RTX5090', 2, None,
    )  # fmt: skip
    assert a['last_event'] == {'at': NOW - 5_000, 'kind': CLEAN_LEASE} and a['benched_reason'] is None
    leased = next(x for x in a['cards'] if x['state'] == LEASED)
    assert leased == {
        'card': card_hash(UUID_A),
        'state': LEASED,
        'since': NOW - 2_400,
        'workload': 'qwen3.8-27b-nvfp4@6',
        'image': 'entrius/qwen3.8-27b-nvfp4:6',
        'leased_at': NOW - 2_400,
        'uptime_s': 2_400.0,
        'healthy': True,
        'draining': False,
        'heartbeat_misses': 1,
        'last_heartbeat_at': NOW - 80,
    }
    draining = next(x for x in a['cards'] if x['state'] == DRAINING)
    assert draining['draining'] is True and draining['healthy'] is False and draining['uptime_s'] == 4_000.0

    # a benched box: no cards, the reason is the bench's standing event, the failed check by name
    assert (b['status'], b['cards'], b['card_count']) == (BENCHED, [], 0)
    assert (b['bench_until'], b['benched_reason'], b['last_failed']) == (NOW + 3_600, CHECK_FAILED, ['gpu_uuid_pin'])
    # a box with no cards yet
    assert (c['status'], c['cards'], c['gpu_type'], c['last_check_at'], c['pay']) == (ADMIT, [], None, None, None)


def test_nothing_private_reaches_the_document(tmp_path):
    text = json.dumps(build(tmp_path))
    for uuid in (UUID_A, UUID_B):
        assert uuid not in text and uuid.removeprefix('GPU-') not in text
    for name, value in PRIVATE.items():
        assert str(value) not in text, name
    for value in ('203.0.113', '198.51.100', 'Permission denied', 'gtp-dev', 'port_map', 'container', 'image_id'):
        assert value not in text, value
    a = next(x for x in json.loads(text)['boxes'] if x['hotkey'] == HK_A)
    assert a['last_failed'] == ['gpu_uuid_pin']  # the check name stays, the error text that rode with it does not


def test_a_card_is_named_by_a_hash_and_an_image_by_repo_and_tag(tmp_path):
    assert card_hash(UUID_A) == card_hash(UUID_A) != card_hash(UUID_B) and len(card_hash(UUID_A)) == 12
    assert public_image(IMAGE) == 'entrius/qwen3.8-27b-nvfp4:6'
    assert public_image('10.0.0.5:5000/private/model:1') is None and public_image(None) is None
    # an instance record that is not this card's (a stale id) is not shown on it
    boxes, instances = fleet()
    instances['i-1'].uuid = UUID_C
    doc = build_fleet(tmp_path, boxes, instances, {}, False, NOW)
    card = next(c for b in doc['boxes'] for c in b['cards'] if c['card'] == card_hash(UUID_A))
    assert 'workload' not in card and doc['controller']['running'] is False and doc['controller']['round_n'] is None


def test_the_write_is_atomic_and_readable_through_a_mount_of_public_alone(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    path = write_fleet(root, build(root))
    assert path == fleet_path(root) == root / 'public' / 'fleet.json'
    assert json.loads(path.read_text())['schema'] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    before = path.stat().st_ino
    write_fleet(root, build(root, NOW + 30))
    assert path.stat().st_ino != before  # renamed over, never rewritten in place
    assert os.listdir(path.parent) == ['fleet.json']  # no tmp file left behind
    with pytest.raises(ValueError):  # NaN is not JSON: refused, and the last good document stays
        write_fleet(root, {'schema': 1, 'generated_at': float('nan')})
    assert json.loads(path.read_text())['generated_at'] == NOW + 30


def test_the_publisher_writes_on_its_interval_and_at_once_when_forced(tmp_path):
    clock = [NOW]
    publisher = Publisher(tmp_path, interval_s=30.0, wall=lambda: clock[0])
    assert publisher.due()
    publisher.write({'schema': 1})
    clock[0] += 29
    assert not publisher.due() and publisher.due(force=True)
    clock[0] += 1
    assert publisher.due()


def test_the_running_controller_publishes_with_the_scorecard_and_on_the_watch_tick(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    now = __import__('time').time()
    StateStore(root / 'boxes.json').put(
        BoxState(HK_A, status=IDLE, pinned_uuids=[UUID_A, UUID_B], card_name='NVIDIA GeForce RTX 5090',
                 last_check_at=now - 60, host=PRIVATE['host'], port=2200,
                 cards={UUID_A: CardState(LEASED, 'i1', now - 60), UUID_B: CardState(IDLE, '', now - 60)})
    )  # fmt: skip
    InstanceStore(root / 'instances.json').put(
        InstanceRecord('i1', 'e@1', HK_A, UUID_A, healthy=True, leased_at=now - 60, host=PRIVATE['host'], port=50085)
    )
    controller = Controller(
        ctl.StateDir(root), Registry(root / 'registry', 'unused'), make_runner=cast(Any, None),
        run_round=cast(Any, None), load_proof=cast(Any, None), intervals=Intervals(),
        oracle=FailSafeOracle(StaticOracle(226.84, 0.003384)), network='test', netuid=422,
    )  # fmt: skip
    controller.settle_once(now - 36)
    controller.settle_once(now)
    controller.scorecard_once(now)
    doc = json.loads(fleet_path(root).read_text())
    assert doc['scorecard']['valid'] and len(doc['scorecard']['sha256']) == 64 and doc['netuid'] == 422
    assert doc['oracle'] == {'tao_usd': 226.84, 'alpha_tao': 0.003384, 'held': False}
    assert doc['rates']['RTX5090']['source'] == 'scorecard'
    assert doc['totals']['cards_by_state'] == {LEASED: 1, IDLE: 1}
    (box,) = doc['boxes']
    assert box['pay']['idle_h'] > 0 and box['pay']['weight'] > 0
    leased = next(c for c in box['cards'] if c['state'] == LEASED)
    assert leased['workload'] == 'e@1' and leased['image'] is None  # an entry the registry cannot read: no image
    assert PRIVATE['host'] not in fleet_path(root).read_text()

    assert controller.publish_once() is False  # inside the interval
    controller.publisher.last_at = now - cfg.PUBLISH_INTERVAL_S - 1
    assert controller.publish_once() is True
    controller.shutdown(grace_s=0)
    assert json.loads(fleet_path(root).read_text())['controller']['running'] is False


def test_gitt_controller_publish_writes_the_document_once(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    boxes, _ = fleet()
    store = StateStore(root / 'boxes.json')
    for box in boxes.values():
        store.put(box)
    shown = invoke('publish', '--state-dir', root, '--allow-dev-keys', '--json')
    payload = json.loads(shown.stdout)
    assert shown.exit_code == 0 and payload['path'] == str(fleet_path(root))
    assert payload['fleet']['totals']['boxes'] == 3 and payload['fleet']['controller']['running'] is False
    assert json.loads(fleet_path(root).read_text()) == payload['fleet']
    assert 'wrote' in invoke('publish', '--state-dir', root, '--allow-dev-keys').output
    assert invoke('publish', '--state-dir', tmp_path / 'nowhere', '--allow-dev-keys').exit_code != 0
