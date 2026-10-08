# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The vendor switch scaffold (vault 30 §11 step 0): a box's vendor is detected at the start of the scrape, carried
on the verdict, pinned at admit and read where a container attaches a card. Every existing box is NVIDIA and nothing
about its path changes; the AMD branches exist and attach device nodes, with nothing yet feeding them."""

import pytest

from gittensor.controller import rentals as rt
from gittensor.controller.checks import vendor as v
from gittensor.controller.checks.catalog import CatalogError, load_catalog, parse_catalog
from gittensor.controller.checks.scrape import GpuInfo, scrape_host
from gittensor.controller.checks.state import ADMIT, IDLE, BoxState, apply_verdict
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.proof import slot
from gittensor.controller.runspec import RunSpec, run_command
from tests.controller.conftest import UUID_5090, passing_runner

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


def test_every_catalog_row_today_is_nvidia_and_an_amd_row_needs_its_ids():
    assert {spec.vendor for spec in load_catalog().values()} == {v.NVIDIA}
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
    assert v.amd_attach_args(['renderD128']) == [
        '--device /dev/kfd',
        '--device /dev/dri/renderD128',
        '--group-add video',
        '--group-add render',
    ]
    assert v.amd_attach_args(whole_box=True)[:2] == ['--device /dev/kfd', '--device /dev/dri']
    # the proof container: one card, its render node
    line = slot.create_command(
        'img', 'AMD-0123456789abcdef', 'gt-proof-0', ['--x'], vendor=v.AMD, render_node='renderD128'
    )
    assert line.startswith('docker create --device /dev/kfd --device /dev/dri/renderD128 --group-add video --group-add render --name gt-proof-0')  # fmt: skip
    assert '--gpus' not in line and '--label io.gittensor.proof.uuid=AMD-0123456789abcdef img --x' in line
    assert slot.create_command('img', 'GPU-1', 'gt-proof-0').startswith('docker create --gpus="device=GPU-1"')
    # the rental pod: every card of the box
    pod = rt.pod_run_command(rt.RentalRecord('rnt_1', uuids=['AMD-1', 'AMD-2'], image='x', vendor=v.AMD))
    assert '--device /dev/kfd --device /dev/dri --group-add video --group-add render' in pod and '--gpus' not in pod
    assert '--label io.gittensor.uuid=AMD-1,AMD-2' in pod
    assert '--gpus \'"device=GPU-1"\'' in rt.pod_run_command(rt.RentalRecord('rnt_1', uuids=['GPU-1'], image='x'))
    # a workload instance: one card
    spec = RunSpec('i-1', 'e@1', 'img@sha256:' + '1' * 64, 'AMD-1', None, vendor=v.AMD, render_node='renderD129')
    assert '--device /dev/kfd --device /dev/dri/renderD129 --group-add video --group-add render' in run_command(spec)
    assert '--gpus "device=GPU-1"' in run_command(RunSpec('i-1', 'e@1', 'img@sha256:' + '1' * 64, 'GPU-1', None))


def test_a_gpu_record_defaults_to_nvidia_with_no_render_node():
    gpu = GpuInfo(
        'GPU-1', 'NVIDIA GeForce RTX 5090', '580.65.06', 32607, 575.0, 575.0, 600.0, '00000000:01:00.0', '12.0'
    )
    assert gpu.vendor == v.NVIDIA and gpu.render_node == '' and gpu.as_dict()['vendor'] == v.NVIDIA
