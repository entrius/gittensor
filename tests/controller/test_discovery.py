# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Discovery over a fake metagraph (a list of ``(hotkey, ip, port, marker)``): a registered compute endpoint is scanned
and admitted, an endpoint that moved with the same host key follows, one with another key is flagged and counts as an
unreachable round, a deregistered box is benched, drained and removed, operator boxes and bad addresses are left
alone; plus `gitt controller discover`, `run --discover` and the marker `gitt up` publishes."""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli: circular import)
from gittensor.agent.config import COMPUTE_AXON_MARKER, COMPUTE_AXON_PROTOCOL, COMPUTE_AXON_SCHEMA, is_compute_axon
from gittensor.controller import cli as ctl
from gittensor.controller.checks.state import ADMIT, BENCHED, LEASED, BoxState, StateStore
from gittensor.controller.daemon import Controller, Intervals
from gittensor.controller.discovery import CHAIN, ChainEndpoint, Discovery, address_problem
from gittensor.controller.reconcile import InstanceStore
from gittensor.controller.ssh import SshTransportError, pinned_host_key
from tests.controller.conftest import UUID_5090
from tests.controller.test_cli import FAKE_PROOF, HK_A, HK_B, KEY_1, KEY_2, NET, box_runner, invoke, runners
from tests.controller.test_placement import FakeDocker, idle_box, make_world, reconciler, seed

# Public addresses (ipaddress calls the RFC 5737 documentation ranges non-global, so they would be ignored).
PUB_A, PUB_A2, PUB_B = '44.10.0.1', '44.10.0.2', '52.20.0.3'


def metagraph(*rows):
    """``(hotkey, ip, port, marker)`` rows as ``ChainEndpoint`` s, the way ``ChainReader.read`` returns them."""
    return [ChainEndpoint(hotkey, ip, port, marker) for hotkey, ip, port, marker in rows]


class Keys:
    """ssh-keyscan over a fake internet: address -> host key; anything else does not answer."""

    def __init__(self, **by_address):
        self.by_address = dict(by_address)
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        key = self.by_address.get(f'{host}:{port}')
        if key is None:
            raise SshTransportError(f'ssh-keyscan {host}:{port}: no ed25519 host key')
        return key


def discovery(root, keys):
    boxes, instances = StateStore(root / 'boxes.json'), InstanceStore(root / 'instances.json')
    return Discovery(boxes, instances, root / 'known_hosts', keys)


@pytest.fixture
def root(tmp_path):
    return tmp_path


def test_only_public_addresses_are_dialled():
    assert address_problem('10.0.0.1', 2200) and address_problem('127.0.0.1', 2200) and address_problem('nope', 1)
    assert address_problem('169.254.169.254', 80) and address_problem('8.8.8.8', 0)
    assert not address_problem(PUB_A, 2200)


def test_a_registered_compute_endpoint_is_scanned_pinned_and_admitted(root):
    keys = Keys(**{f'{PUB_A}:2200': KEY_1})
    report = discovery(root, keys).run_pass(
        metagraph(
            (HK_A, PUB_A, 2200, True),
            (HK_B, PUB_B, 8091, False),  # a phase-0 serving miner's axon: not ours
            ('5Private', '10.0.0.5', 2200, True),  # never dialled
            ('5NotServing', '0.0.0.0', 0, False),
        )
    )
    assert [(a.kind, a.hotkey) for a in report.actions] == [('admit', HK_A)]
    assert (report.registered, report.compute) == (4, 2)
    assert '10.0.0.5 is not a public address' in report.ignored['5Private']
    assert keys.calls == [(PUB_A, 2200)]
    box = StateStore(root / 'boxes.json').get(HK_A)
    assert (box.status, box.source, box.host, box.port, box.host_key) == (ADMIT, CHAIN, PUB_A, 2200, KEY_1)
    assert pinned_host_key(root / 'known_hosts', PUB_A, 2200) == KEY_1

    # the next read: nothing to scan, nothing to change
    again = discovery(root, keys).run_pass(metagraph((HK_A, PUB_A, 2200, True)))
    assert again.actions == [] and keys.calls == [(PUB_A, 2200)]


def test_a_box_that_does_not_answer_is_not_created(root):
    report = discovery(root, Keys()).run_pass(metagraph((HK_A, PUB_A, 2200, True)))
    assert [(a.kind, a.ok) for a in report.actions] == [('scan_failed', False)] and not report.ok
    assert StateStore(root / 'boxes.json').boxes == {}


def test_an_address_another_box_holds_is_not_scanned_or_repinned(root):
    StateStore(root / 'boxes.json').put(BoxState(HK_A, source='operator', host=PUB_A, port=2200, host_key=KEY_1))
    keys = Keys(**{f'{PUB_A}:2200': KEY_2})
    report = discovery(root, keys).run_pass(metagraph((HK_B, PUB_A, 2200, True)))
    assert [(a.kind, a.hotkey) for a in report.actions] == [('conflict', HK_B)]
    assert HK_B not in StateStore(root / 'boxes.json').boxes


def test_an_endpoint_that_moved_with_the_pinned_key_follows_it(root):
    keys = Keys(**{f'{PUB_A}:2200': KEY_1, f'{PUB_A2}:2201': KEY_1})
    discovery(root, keys).run_pass(metagraph((HK_A, PUB_A, 2200, True)))
    report = discovery(root, keys).run_pass(metagraph((HK_A, PUB_A2, 2201, True)))
    assert [a.kind for a in report.actions] == ['moved']
    box = StateStore(root / 'boxes.json').get(HK_A)
    assert (box.host, box.port, box.endpoint_changed) == (PUB_A2, 2201, {})
    assert pinned_host_key(root / 'known_hosts', PUB_A2, 2201) == KEY_1
    assert pinned_host_key(root / 'known_hosts', PUB_A, 2200) == ''


def test_an_endpoint_with_another_key_is_flagged_counts_unreachable_and_takes_no_instance(root, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'gt_ca').write_text('placeholder\n')
    keys = Keys(**{f'{PUB_A}:2200': KEY_1, f'{PUB_A2}:2200': KEY_2})
    discovery(state, keys).run_pass(metagraph((HK_A, PUB_A, 2200, True)))
    report = discovery(state, keys).run_pass(metagraph((HK_A, PUB_A2, 2200, True)))
    (flag,) = report.actions
    assert (flag.kind, flag.ok) == ('endpoint_changed', False)
    assert KEY_2 in flag.detail and 'not re-pinned' in flag.detail
    box = StateStore(state / 'boxes.json').get(HK_A)
    assert (box.host, box.host_key) == (PUB_A, KEY_1)  # nothing re-pinned
    assert box.endpoint_changed['host'] == PUB_A2 and box.endpoint_changed['host_key'] == KEY_2
    assert discovery(state, keys).run_pass(metagraph((HK_A, PUB_A2, 2200, True))).actions == []  # said once

    # the round: no visit, an unreachable round each time, three bench it
    runner = box_runner()
    for _ in range(3):
        with runners({HK_A: runner}):
            result = invoke('round', '--state-dir', state, '--proof', FAKE_PROOF, *NET, '--json')
        assert result.exit_code == 2, result.output
        row = json.loads(result.stdout)['boxes'][0]
        assert row['transport_error'].startswith('endpoint_changed: chain publishes 44.10.0.2:2200')
    assert runner.calls == []
    after = StateStore(state / 'boxes.json').get(HK_A)
    assert after.status == BENCHED and after.unreachable_count == 3

    # placement never picks an IDLE box whose endpoint changed
    world_box = idle_box('hkX', uuids=(UUID_5090,))
    world_box.endpoint_changed = {'host': PUB_B, 'port': 2200, 'host_key': '', 'at': 1.0}
    (tmp_path / 'w').mkdir()
    wroot, registry = make_world(tmp_path / 'w')
    seed(wroot, world_box, replicas=1)
    placed = reconciler(wroot, registry, {'hkX': FakeDocker()}).run_pass()
    assert placed.actions == [] and 'no IDLE card fits' in placed.errors[0]

    # the operator verifies the box and re-pins: the flag clears
    with patch.object(ctl, '_scan_host_key', return_value=KEY_2):
        rekey = invoke('admit', HK_A, '--host', PUB_A2, '--port', 2200, '--force-rekey', '--state-dir', state)
    assert rekey.exit_code == 0, rekey.output
    fixed = StateStore(state / 'boxes.json').get(HK_A)
    assert (fixed.host, fixed.host_key, fixed.endpoint_changed, fixed.source) == (PUB_A2, KEY_2, {}, CHAIN)
    assert discovery(state, keys).run_pass(metagraph((HK_A, PUB_A2, 2200, True))).actions == []


def test_the_pinned_key_answering_at_the_new_address_later_clears_the_flag(root):
    keys = Keys(**{f'{PUB_A}:2200': KEY_1})
    discovery(root, keys).run_pass(metagraph((HK_A, PUB_A, 2200, True)))
    assert discovery(root, keys).run_pass(metagraph((HK_A, PUB_A2, 2200, True))).actions[0].kind == 'endpoint_changed'
    keys.by_address[f'{PUB_A2}:2200'] = KEY_1  # the box came up at its new address
    assert [a.kind for a in discovery(root, keys).run_pass(metagraph((HK_A, PUB_A2, 2200, True))).actions] == ['moved']
    # and a chain that goes back to the pinned address clears a flag too
    keys.by_address[f'{PUB_B}:2200'] = KEY_2
    discovery(root, keys).run_pass(metagraph((HK_A, PUB_B, 2200, True)))
    report = discovery(root, keys).run_pass(metagraph((HK_A, PUB_A2, 2200, True)))
    assert [a.kind for a in report.actions] == ['endpoint_restored']
    assert StateStore(root / 'boxes.json').get(HK_A).endpoint_changed == {}


def test_a_deregistered_box_is_benched_drained_by_the_reconciler_then_removed(tmp_path):
    root, registry = make_world(tmp_path)
    box_state = idle_box(HK_A, uuids=(UUID_5090,))
    box_state.source = CHAIN
    seed(root, box_state, replicas=1)
    (root / 'known_hosts').write_text(f'[10.0.0.1]:2200 {KEY_1}\n')
    box = FakeDocker()
    assert reconciler(root, registry, {HK_A: box}).run_pass().ok
    assert StateStore(root / 'boxes.json').get(HK_A).cards[UUID_5090].state == LEASED

    keys = Keys()
    report = discovery(root, keys).run_pass(metagraph((HK_B, PUB_B, 8091, False)))  # HK_A is gone from the metagraph
    assert [(a.kind, a.detail) for a in report.actions] == [('deregistered', 'BENCHED; 1 instance(s) to drain')]
    benched = StateStore(root / 'boxes.json').get(HK_A)
    assert benched.status == BENCHED and benched.bench_until is None and benched.cards == {}

    drained = reconciler(root, registry, {HK_A: box}).run_pass()
    assert [a.kind for a in drained.actions] == ['drain'] and box.containers == {}

    removed = discovery(root, keys).run_pass(metagraph((HK_B, PUB_B, 8091, False)))
    assert [a.kind for a in removed.actions] == ['removed'] and HK_A not in StateStore(root / 'boxes.json').boxes
    assert (root / 'known_hosts').read_text() == '' and keys.calls == []


def test_operator_boxes_are_never_moved_or_removed(root):
    StateStore(root / 'boxes.json').put(BoxState(HK_A, source='operator', host='10.0.0.1', port=2200, host_key=KEY_1))
    StateStore(root / 'boxes.json').put(BoxState(HK_B, host='10.0.0.2', port=2200, host_key=KEY_2))  # an old file: ''
    keys = Keys(**{f'{PUB_A}:2200': KEY_1})
    report = discovery(root, keys).run_pass(metagraph((HK_A, PUB_A, 2200, True)))
    assert report.actions == [] and keys.calls == []
    assert set(StateStore(root / 'boxes.json').boxes) == {HK_A, HK_B}


def test_discover_command_and_run_discover(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'gt_ca').write_text('placeholder\n')
    chain = SimpleNamespace(read=lambda: metagraph((HK_A, PUB_A, 2200, True)))
    with (
        patch.object(ctl, '_chain_reader', return_value=chain) as reader,
        patch.object(ctl, '_scan_host_key', return_value=KEY_1),
    ):
        result = invoke('discover', '--network', 'test', '--netuid', 422, '--state-dir', state, '--json')
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (
        payload['netuid'] == 422
        and payload['network'].startswith('wss://test.')
        and payload['actions'][0]['kind'] == 'admit'
    )
    assert reader.call_args.args == ('wss://test.finney.opentensor.ai:443', 422)
    assert StateStore(state / 'boxes.json').get(HK_A).source == CHAIN

    def broken():
        raise ConnectionError('rpc down')

    with patch.object(ctl, '_chain_reader', return_value=SimpleNamespace(read=broken)):
        failed = invoke('discover', '--network', 'test', '--state-dir', state)
    assert failed.exit_code == 2 and 'rpc down' in failed.output

    # one process: the discover loop admits a box the round then picks up
    reads = []
    controller = Controller(
        ctl.StateDir(tmp_path / 'daemon').ensure(),
        registry=cast(Any, None),
        make_runner=lambda box, purpose: cast(Any, None),
        run_round=lambda proof, **shared: None,
        load_proof=lambda: None,
        read_chain=lambda: reads.append(1) or metagraph((HK_B, PUB_B, 2200, True)),
        scan_host_key=lambda host, port: KEY_2,
        intervals=Intervals(discover_s=60),
    )
    report = controller.discover_once()
    assert report is not None
    assert [a.kind for a in report.actions] == ['admit'] and controller.boxes.boxes[HK_B].status == ADMIT
    status = json.loads((tmp_path / 'daemon' / 'controller.json').read_text())
    assert status['discover']['actions'][0]['hotkey'] == HK_B and status['intervals']['discover_s'] == 60

    def down():
        raise ConnectionError('rpc down')

    controller.read_chain = down
    assert controller.discover_once() is None and HK_B in controller.boxes.boxes  # a failed read changes nothing


def test_the_marker_gitt_up_publishes_is_the_one_discovery_reads():
    axon = SimpleNamespace(
        hotkey=HK_A,
        ip=PUB_A,
        port=2200,
        protocol=COMPUTE_AXON_PROTOCOL,
        placeholder1=COMPUTE_AXON_MARKER,
        placeholder2=COMPUTE_AXON_SCHEMA,
    )
    assert ChainEndpoint.from_axon(axon) == ChainEndpoint(HK_A, PUB_A, 2200, True)
    assert not is_compute_axon(4, 0, 0)  # a phase-0 miner axon
    assert all(0 <= v <= 255 for v in (COMPUTE_AXON_PROTOCOL, COMPUTE_AXON_MARKER, COMPUTE_AXON_SCHEMA))  # u8 on chain
