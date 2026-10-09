# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The vendor switch scaffold (vault 30 §11 step 0): a box's vendor is detected at the start of the scrape, carried
on the verdict, pinned at admit and read where a container attaches a card. Every existing box is NVIDIA and nothing
about its path changes; the AMD branches exist and attach device nodes, with nothing yet feeding them."""

import pytest

from gittensor.controller import rentals as rt
from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import vendor as v
from gittensor.controller.checks import why as w
from gittensor.controller.checks.catalog import CatalogError, load_catalog, parse_catalog
from gittensor.controller.checks.full_check import run_full_check
from gittensor.controller.checks.scrape import GpuInfo, scrape_host
from gittensor.controller.checks.state import ADMIT, IDLE, BoxState, StateStore, apply_verdict
from gittensor.controller.checks.verdict import BENCH, CheckResult, CheckVerdict
from gittensor.controller.proof import slot
from gittensor.controller.publish import build_fleet
from gittensor.controller.runspec import RunSpec, run_command
from tests.controller.conftest import CONFIG, UUID_5090, passing_runner
from tests.controller.test_rentals import HK, NOW, U1, U2, Clock, order, pod_runner, reconciler, rentable_box

NODES = {'AMD-1': 'renderD128', 'AMD-2': 'renderD129'}

AMD_ROW = {'names': ['AMD Instinct MI300X'], 'compute_cap': '', 'vram_mib': 196608, 'status': 'listed'}


def test_the_vendor_is_read_off_the_loaded_kernel_module():
    assert v.parse_vendor('nvidia\n') == v.NVIDIA
    assert v.parse_vendor('amdgpu\n') == v.AMD
    assert v.parse_vendor('nvidia\namdgpu\n') == v.BOTH
    assert v.parse_vendor('') == '' and v.parse_vendor('junk\n') == ''
    # Only a box that is AMD and nothing else takes the AMD avenue; the rest stay on the NVIDIA path, which fails
    # closed by itself when there is no NVIDIA card.
    assert v.vendor_or_default(v.AMD) == v.AMD
    assert all(v.vendor_or_default(d) == v.NVIDIA for d in (v.NVIDIA, v.BOTH, '', 'intel'))


def test_the_scrape_detects_the_vendor_first_and_defaults_to_nvidia():
    runner = passing_runner()
    scrape = scrape_host(runner)
    assert runner.calls[0] == v.VENDOR_DETECT_COMMAND
    assert scrape.vendor_detected == v.NVIDIA and scrape.vendor == v.NVIDIA and 'vendor' not in scrape.errors
    # A box whose detect step fails (an old agent, a dead command) is judged as NVIDIA, as every box was.
    runner.on(v.VENDOR_DETECT_COMMAND, Exception('gone'))
    scrape = scrape_host(runner)
    assert scrape.vendor == v.NVIDIA and 'vendor' in scrape.errors
    runner.on(v.VENDOR_DETECT_COMMAND, 'amdgpu\n')
    assert scrape_host(runner).vendor == v.AMD


def test_the_verdict_carries_the_vendor_and_admit_pins_it():
    passed = [CheckResult('gpu_spec', True, {})]
    verdict = CheckVerdict.from_checks(passed, [UUID_5090], 'NVIDIA GeForce RTX 5090', '580.65.06', vendor=v.AMD)
    assert verdict.vendor == v.AMD and verdict.as_dict()['vendor'] == v.AMD
    assert CheckVerdict.from_checks(passed, [UUID_5090]).vendor == v.NVIDIA
    box = apply_verdict(BoxState('hk', ADMIT), verdict, now=1000.0)
    assert box.status == IDLE and box.vendor == v.AMD and box.pinned_uuids == [UUID_5090]
    # A later pass never re-pins: the vendor is the admit's.
    again = CheckVerdict.from_checks(passed, [UUID_5090], vendor=v.NVIDIA)
    assert apply_verdict(box, again, now=2000.0).vendor == v.AMD
    # A boxes.json from before the field existed loads as nvidia.
    assert BoxState.from_dict({'box_id': 'hk', 'status': IDLE, 'pinned_uuids': [UUID_5090]}).vendor == v.NVIDIA


def test_an_amd_catalog_row_needs_its_ids_and_an_nvidia_row_none():
    assert {spec.vendor for spec in load_catalog().values()} == {v.NVIDIA, v.AMD}
    assert all(spec.pci_ids and spec.gfx_target for spec in load_catalog().values() if spec.vendor == v.AMD)
    spec = parse_catalog({'MI300X': {**AMD_ROW, 'vendor': 'amd', 'pci_ids': ['0x74A1'], 'gfx_target': 'GFX942'}})[
        'MI300X'
    ]
    assert spec.vendor == v.AMD and spec.pci_ids == ('0x74a1',) and spec.gfx_target == 'gfx942'
    assert parse_catalog({'X': {**AMD_ROW, 'names': ['x']}})['X'].vendor == v.NVIDIA  # the default
    with pytest.raises(CatalogError, match='vendor must be one of'):
        parse_catalog({'X': {**AMD_ROW, 'vendor': 'intel', 'pci_ids': ['0x1'], 'gfx_target': 'x'}})
    with pytest.raises(CatalogError, match='needs pci_ids and gfx_target'):
        parse_catalog({'X': {**AMD_ROW, 'vendor': 'amd'}})


def test_an_amd_card_is_attached_by_device_nodes_and_an_nvidia_card_by_gpus():
    assert v.render_node_path('renderD128') == '/dev/dri/renderD128'
    assert v.render_node_path('129') == '/dev/dri/renderD129' and v.render_node_path('/dev/dri/renderD130') == '/dev/dri/renderD130'  # fmt: skip
    # the gids are the host's, numeric (names resolve inside the container, where a stock image has no `render`);
    # none when the box did not report them: a root container needs none
    assert v.amd_attach_args(['renderD128'], [44, 992]) == [
        '--device /dev/kfd',
        '--device /dev/dri/renderD128',
        '--group-add 44',
        '--group-add 992',
    ]
    assert v.amd_attach_args([]) == ['--device /dev/kfd']  # never all of /dev/dri
    # the proof container: one card, its render node
    line = slot.create_command(
        'img', 'AMD-0123456789abcdef', 'gt-proof-0', ['--x'], vendor=v.AMD, render_node='renderD128', gids=(44, 992)
    )
    assert line.startswith('docker create --device /dev/kfd --device /dev/dri/renderD128 --group-add 44 --group-add 992 --name gt-proof-0')  # fmt: skip
    assert '--group-add' not in slot.create_command('img', 'AMD-1', 'gt-proof-0', vendor=v.AMD, render_node='renderD128')  # fmt: skip
    assert '--gpus' not in line and '--label io.gittensor.proof.uuid=AMD-0123456789abcdef img --x' in line
    assert slot.create_command('img', 'GPU-1', 'gt-proof-0').startswith('docker create --gpus="device=GPU-1"')
    # the rental pod: every card of the box by its pinned render node, and no pod without the pin (31 §2 #2)
    pod = rt.pod_run_command(rt.RentalRecord('rnt_1', uuids=['AMD-1', 'AMD-2'], image='x', vendor=v.AMD, render_nodes=NODES, amd_gids=[44, 992]))  # fmt: skip
    assert '--device /dev/kfd --device /dev/dri/renderD128 --device /dev/dri/renderD129 --group-add 44 --group-add 992' in pod  # fmt: skip
    assert '--gpus' not in pod and '/dev/dri ' not in pod and '--label io.gittensor.uuid=AMD-1,AMD-2' in pod
    with pytest.raises(rt.RentalError, match='no render node pinned for 1 of 2'):
        rt.pod_run_command(rt.RentalRecord('rnt_1', uuids=['AMD-1', 'AMD-2'], image='x', vendor=v.AMD, render_nodes={'AMD-1': 'renderD128'}))  # fmt: skip
    assert '--gpus \'"device=GPU-1"\'' in rt.pod_run_command(rt.RentalRecord('rnt_1', uuids=['GPU-1'], image='x'))
    # a workload instance: one card
    spec = RunSpec('i-1', 'e@1', 'img@sha256:' + '1' * 64, 'AMD-1', None, vendor=v.AMD, render_node='renderD129')
    assert '--device /dev/kfd --device /dev/dri/renderD129 ' in run_command(spec) and '--group-add video' not in run_command(spec)  # fmt: skip
    assert '--gpus "device=GPU-1"' in run_command(RunSpec('i-1', 'e@1', 'img@sha256:' + '1' * 64, 'GPU-1', None))


def test_a_gpu_record_defaults_to_nvidia_with_no_render_node():
    gpu = GpuInfo(
        'GPU-1', 'NVIDIA GeForce RTX 5090', '580.65.06', 32607, 575.0, 575.0, 600.0, '00000000:01:00.0', '12.0'
    )
    assert gpu.vendor == v.NVIDIA and gpu.render_node == '' and gpu.as_dict()['vendor'] == v.NVIDIA


def test_a_box_with_both_vendors_is_refused_by_name(proof, allowlist):
    """31 §2 #4 (Alex 10/8: no mixed boxes, as no mixed types). Before, a both-modules box read as a broken nvidia-smi,
    which a miner cannot act on; now the ``vendor`` check names it. No module at all still passes here and fails
    closed in ``gpu_spec``."""
    assert all(ck.check_vendor(d).passed for d in (v.NVIDIA, v.AMD, ''))
    both = ck.check_vendor(v.BOTH)
    assert not both.passed and both.name == ck.VENDOR and both.evidence['vendor'] == v.NVIDIA
    assert w.from_results([both]) == {ck.VENDOR: w.PHRASES[w.VENDOR_MIXED]} and ck.VENDOR in w.BY_NAME
    runner = passing_runner()
    runner.on(v.VENDOR_DETECT_COMMAND, 'nvidia\namdgpu\n')
    verdict = run_full_check(runner, allowlist, proof, pinned_uuids=None, config=CONFIG, now=1_000.0)
    assert verdict.verdict == BENCH and verdict.failed == [ck.VENDOR] and verdict.vendor == v.NVIDIA
    assert verdict.check(ck.GPU_PROOF).skipped  # type: ignore[union-attr]
    box = apply_verdict(BoxState('hk', ADMIT), verdict, now=1_000.0)
    assert box.last_failed == [ck.VENDOR] and box.last_failed_why == {ck.VENDOR: w.PHRASES[w.VENDOR_MIXED]}


def test_the_render_nodes_are_pinned_at_every_passing_check_like_the_power_baseline():
    """31 §2 #3: the nodes a pod or a proof is given are the ones the scrape saw on that card. Minors are stable
    within a boot and may change across one, so every passing full check re-pins the map, not the admit alone."""
    passed = [CheckResult('gpu_spec', True, {})]
    verdict = CheckVerdict.from_checks(passed, list(NODES), 'AMD Instinct MI300X', vendor=v.AMD, render_nodes=NODES)
    assert verdict.as_dict()['render_nodes'] == NODES
    box = apply_verdict(BoxState('hk', ADMIT), verdict, now=1_000.0)
    assert box.status == IDLE and box.vendor == v.AMD and box.render_nodes == NODES
    renumbered = {'AMD-1': 'renderD129', 'AMD-2': 'renderD130'}  # a reboot
    again = CheckVerdict.from_checks(passed, list(NODES), vendor=v.AMD, render_nodes=renumbered)
    assert apply_verdict(box, again, now=2_000.0).render_nodes == renumbered
    # a failing check leaves the last good pin; an NVIDIA box pins nothing; a hand-edited file reads as nothing
    failed = CheckVerdict.from_checks([CheckResult('gpu_spec', False, {})], list(NODES), vendor=v.AMD)
    assert apply_verdict(box, failed, now=3_000.0).render_nodes == NODES
    nvidia = apply_verdict(BoxState('hk2', ADMIT), CheckVerdict.from_checks(passed, [UUID_5090]), now=1_000.0)
    assert nvidia.render_nodes == {} and 'render_nodes' not in nvidia.identity
    assert BoxState.from_dict({'box_id': 'hk', 'status': IDLE, 'identity': {'render_nodes': 5}}).render_nodes == {}


def test_placement_copies_the_pin_to_the_rental_and_skips_an_amd_box_without_one(tmp_path):
    nodes = {U1: 'renderD128', U2: 'renderD129'}
    boxes = StateStore(tmp_path / 'boxes.json')
    boxes.put(rentable_box(vendor=v.AMD, identity={'render_nodes': nodes}))
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    report = rec.run_pass()
    r = store.rentals[r.id]
    assert [a.kind for a in report.actions] == ['place', 'active'] and r.state == rt.ACTIVE
    assert r.vendor == v.AMD and r.render_nodes == nodes and r.uuids == [U1, U2]
    line = next(c for c in runner.calls if c.startswith('docker run -d'))
    assert '--device /dev/kfd --device /dev/dri/renderD128 --device /dev/dri/renderD129' in line and '--group-add video' not in line  # fmt: skip
    assert '--gpus' not in line and '--runtime=sysbox-runc' in line
    # the record round-trips through rentals.json
    assert rt.RentalStore(tmp_path / 'rentals.json').rentals[r.id].render_nodes == nodes
    # an AMD box with no pin (an old boxes.json, a check that never saw the nodes) is not placed
    boxes.put(rentable_box('5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw', vendor=v.AMD, uid=46))
    r2 = order(store)
    rec.run_pass()
    assert store.rentals[r2.id].state == rt.REQUESTED
    # an NVIDIA box places as it always has: no nodes, --gpus
    boxes.put(rentable_box('5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty', uid=47))
    rec.run_pass()
    r2 = store.rentals[r2.id]
    assert r2.state == rt.ACTIVE and r2.vendor == v.NVIDIA and r2.render_nodes == {}


def test_the_vendor_is_published_per_box(tmp_path):
    doc = build_fleet(tmp_path, {HK: rentable_box(vendor=v.AMD)}, {}, {}, False, NOW)
    assert doc['boxes'][0]['vendor'] == v.AMD
    doc = build_fleet(tmp_path, {HK: rentable_box()}, {}, {}, False, NOW)
    assert doc['boxes'][0]['vendor'] == v.NVIDIA
