# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The AMD avenue in the controller (vault 30 §3, 31 step 3), on the MI325X fixtures (fixtures/amd/README.md: the 1-card
file is the 10/9 droplet's real capture, the 8-card file is derived from it):
the sysfs pass and its parsers, a box going ADMIT → full check → proof by device nodes → IDLE with its render nodes
and stack pinned, the refusals a partitioned card, a zero serial, an old stack, a listed type and a wrong box size
each earn with their own phrase, the KFD holder scan, the heartbeat re-reading the partition and the render node,
and the rows the catalog, the pay table and the fleet page carry. The proof's numbers come from the same droplet
run (gt-proof)."""

from gittensor.controller import heartbeat as hb
from gittensor.controller.checks import amd_scrape as a
from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import why as w
from gittensor.controller.checks.catalog import LISTED, load_catalog, spec_for_pci_id
from gittensor.controller.checks.full_check import run_full_check
from gittensor.controller.checks.runner import FakeRunner
from gittensor.controller.checks.scrape import (
    AMD_DEVICE_HOLDERS_COMMAND,
    AMD_GPU_DEVICE,
    nvidia_smi_command,
    parse_device_holders,
    scrape_host,
)
from gittensor.controller.checks.state import ADMIT, IDLE, BoxState, apply_verdict
from gittensor.controller.checks.vendor import AMD, NVIDIA
from gittensor.controller.checks.verdict import BENCH, CheckVerdict
from gittensor.controller.pay.rates import load_rates
from gittensor.controller.publish import build_fleet
from gittensor.controller.reconcile import InstanceRecord
from tests.controller.conftest import (
    AMD_UUIDS,
    CONFIG,
    CONFIG_AMD,
    NETWORK_TARGETS,
    NO_DEVICE_HOLDERS,
    fixture,
    passing_amd_runner,
)
from tests.controller.test_rentals import HK, NOW, rentable_box

SYSFS_1 = fixture('amd/sysfs_mi325x_1.txt')
SYSFS_8 = fixture('amd/sysfs_mi325x_8.txt')
AMD_CHECKS = [
    ck.VENDOR, ck.GPU_SPEC, ck.GPU_UUID_PIN, ck.AMD_STACK, ck.POWER_LIMIT, ck.AGENT_IMAGE, ck.DISK_FREE,
    ck.CARD_FREE, ck.NETWORK, ck.GPU_PROOF,
]  # fmt: skip


def check(verdict: CheckVerdict, name: str):
    result = verdict.check(name)
    assert result is not None, name
    return result


def why_of(verdict: CheckVerdict, name: str) -> str:
    return w.from_results(verdict.checks)[name]


# ---------------------------------------------------------------- the sysfs pass -------------------------------------


def test_the_sysfs_pass_yields_the_cards_joined_to_their_kfd_nodes():
    cards, stack = a.parse_amd_sysfs(SYSFS_1)
    (c,) = cards
    assert c.render_node == 'renderD129' and c.pci == '0000:83:00.0' and c.uuid == AMD_UUIDS[0]
    assert (
        c.device_id == '0x74b9'
        and c.product_name == ''  # the droplet's driver publishes none; the catalog names the card
        and a.AMD_VENDOR_ID in a.AMD_SYSFS_COMMAND
    )
    assert c.memory_total_mib == 261_824 and c.vram_bytes == 274_542_362_624
    assert (c.power_cap_w, c.power_cap_default_w, c.power_cap_max_w) == (1000.0, 1000.0, 1000.0)
    assert c.partition == 'SPX/NPS1' and c.whole and c.vbios == '113-M3250101-100'
    # the KFD node joined on the render minor: the gfx target, and the same serial in decimal
    assert c.gfx_target == 'gfx942' and c.kfd_unique_id == c.unique_id and c.id_ok
    assert stack.kernel == '6.8.0-137-generic' and stack.amdgpu == '6.19.14.31400000'
    assert stack.as_dict() == {'kernel': '6.8.0-137-generic', 'amdgpu': '6.19.14.31400000'}
    eight, _ = a.parse_amd_sysfs(SYSFS_8)
    assert [c.render_node for c in eight] == [f'renderD{129 + i}' for i in range(8)]
    assert [c.uuid for c in eight] == AMD_UUIDS and len({c.pci for c in eight}) == 8
    # a card whose KFD node names another serial is not usable; one with no partition files is whole
    decimal = str(int(AMD_UUIDS[0][4:], 16))  # KFD prints the serial in decimal
    assert f'unique_id {decimal}' in SYSFS_1
    (odd,) = a.parse_amd_sysfs(SYSFS_1.replace(f'unique_id {decimal}', 'unique_id 99'))[0]
    assert not odd.id_ok and odd.kfd_unique_id == f'{99:016x}'
    (consumer,) = a.parse_amd_sysfs(SYSFS_1.replace('current_compute_partition=SPX', 'current_compute_partition=').replace('current_memory_partition=NPS1', 'current_memory_partition='))[0]  # fmt: skip
    assert consumer.whole and consumer.partition == '-/-'
    assert a.parse_amd_sysfs('') == ([], a.AmdStack())


def test_gfx_targets_decode_from_the_kfd_version_number():
    assert [a.decode_gfx_target(v) for v in (90402, 90010, 90500, 110000, 120001, 0)] == [
        'gfx942', 'gfx90a', 'gfx950', 'gfx1100', 'gfx1201', '',
    ]  # fmt: skip
    assert a.version_tuple('6.8.0-137-generic') == (6, 8) and a.version_tuple('6.10.5') == (6, 10)
    assert a.version_tuple('6.19.14.31400000') == (6, 19)  # the DKMS driver's version string on the droplet
    assert a.version_tuple('') == (0, 0) and a.version_tuple('garbage') == (0, 0)


def test_the_amd_scrape_asks_sysfs_and_opens_nothing_on_the_card():
    """30 §3: nothing on the AMD path opens /dev/kfd (a reset kills every holder; the agent must never be one). The
    holder scan names the node in a `find -lname` test, which opens nothing; every other command is sysfs."""
    runner = passing_amd_runner()
    scrape = scrape_host(runner, network_targets=NETWORK_TARGETS)
    assert scrape.vendor == AMD and scrape.errors == {} and nvidia_smi_command() not in runner.calls
    for command in runner.calls:
        if '/dev/kfd' in command or '/dev/dri' in command:
            assert command == AMD_DEVICE_HOLDERS_COMMAND, command
    (g,) = scrape.gpus
    assert g.uuid == AMD_UUIDS[0] and g.vendor == AMD and g.render_node == 'renderD129'
    assert g.name == 'AMD Instinct MI325X' and g.compute_cap == 'gfx942'  # the catalog's name, KFD's target
    assert g.memory_total_mib == 261_824 and g.power_limit_w == 1000.0 and g.driver == '6.8.0-137-generic'
    assert scrape.driver == '6.8.0-137-generic' and scrape.amd_stack is not None and len(scrape.amd_cards) == 1
    # a card no row lists keeps a name that says so; the id, not the name, is what the spec check judges
    unknown = scrape_host(
        passing_amd_runner(sysfs=SYSFS_1.replace('device=0x74b9', 'device=0x7777')), network_targets=()
    )
    assert unknown.gpus[0].name == 'AMD 0x7777' and spec_for_pci_id('0x7777') is None
    # a dead transport on the sysfs step fails that step and leaves no cards
    dead = scrape_host(
        FakeRunner()
        .on(a.AMD_SYSFS_COMMAND, ConnectionError('reset'))
        .on('for m in nvidia amdgpu; do test -d /sys/module/$m && echo $m; done; true', 'amdgpu\n'),
        network_targets=(),
    )
    assert dead.vendor == AMD and dead.gpus == [] and 'ConnectionError' in dead.errors['amd_sysfs']


# ---------------------------------------------------------------- the full check -------------------------------------


def test_an_mi325x_box_is_admitted_proved_by_device_nodes_and_pinned(proof, allowlist):
    runner = passing_amd_runner()
    verdict = run_full_check(runner, allowlist, proof, pinned_uuids=None, config=CONFIG_AMD, now=1_000.0)
    assert verdict.verdict == ADMIT and verdict.failed == [] and verdict.vendor == AMD
    assert [c.name for c in verdict.checks] == AMD_CHECKS and all(c.passed for c in verdict.checks)
    assert verdict.gpu_uuids == AMD_UUIDS[:1] and verdict.card_name == 'AMD Instinct MI325X'
    assert verdict.render_nodes == {AMD_UUIDS[0]: 'renderD129'}
    spec = check(verdict, ck.GPU_SPEC).evidence
    assert spec['gpu_type'] == 'MI325X' and spec['gfx_target'] == 'gfx942' and spec['partition'] == {AMD_UUIDS[0]: 'SPX/NPS1'}  # fmt: skip
    assert (
        spec['product_names'] == [] and 261_824 < spec['gpus'][0]['memory_total_mib'] + 1
    )  # 0.3 % under nominal is in the window
    stack = check(verdict, ck.AMD_STACK).evidence
    assert stack['passed_on'] == 'kernel' and stack['record']['kernel'] == '6.8.0-137-generic'
    # the proof container: attached by device nodes, the HIP image, never --gpus
    (create,) = [c for c in runner.calls if c.startswith('docker create')]
    assert create.startswith('docker create --device /dev/kfd --device /dev/dri/renderD129 --name gt-proof-0')
    assert '--group-add' not in create  # a group name does not resolve inside the image, and Sysbox ignores group bits
    assert '--gpus' not in create and 'entrius/gt-proof-rocm:test' in create and f'io.gittensor.proof.uuid={AMD_UUIDS[0]}' in create  # fmt: skip
    assert verdict.check(ck.NVML_DIGEST) is None  # no NVML allowlist on AMD: the stack record and its floor instead
    # state: vendor, render nodes and the stack pinned beside the power baseline
    box = apply_verdict(BoxState('hk', ADMIT), verdict, now=1_000.0)
    assert box.status == IDLE and box.vendor == AMD and box.pinned_uuids == AMD_UUIDS[:1]
    assert box.render_nodes == {AMD_UUIDS[0]: 'renderD129'} and box.identity['power_limits'] == {AMD_UUIDS[0]: 1000.0}
    assert box.identity['amd_stack'] == {'kernel': '6.8.0-137-generic', 'amdgpu': '6.19.14.31400000', 'vbios': {AMD_UUIDS[0]: '113-M3250101-100'}}  # fmt: skip


def test_an_eight_card_box_is_one_box_and_every_card_gets_its_own_node(proof, allowlist):
    runner = passing_amd_runner(sysfs=SYSFS_8)
    verdict = run_full_check(runner, allowlist, proof, pinned_uuids=None, config=CONFIG_AMD, now=1_000.0)
    assert verdict.verdict == ADMIT and verdict.gpu_uuids == AMD_UUIDS
    creates = [c for c in runner.calls if c.startswith('docker create')]
    assert [c.split('--device /dev/dri/')[1].split(' ')[0] for c in creates] == [f'renderD{129 + i}' for i in range(8)]
    assert verdict.render_nodes == {u: f'renderD{129 + i}' for i, u in enumerate(AMD_UUIDS)}
    # four cards is not a size the MI325X row admits (the market sells 8x; 1x is the measurement VM)
    four = '\n'.join(
        line
        for line in SYSFS_8.splitlines()
        if not any(f'renderD{129 + i}' in line or f'-- {i + 1}' == line for i in range(4, 8))
    )
    verdict = run_full_check(passing_amd_runner(sysfs=four), allowlist, proof, pinned_uuids=None, config=CONFIG_AMD)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_SPEC]
    assert why_of(verdict, ck.GPU_SPEC) == 'this box reports 4 GPUs, not a box size the pool admits for its GPU type'


def test_a_listed_amd_type_is_not_admitted_until_the_row_flips(proof, allowlist):
    """An AMD row is listed until its digest run flips it: an MI300X (the droplet's card with the MI300X device id) on the
    catalog as shipped benches, and the phrase names the type, not a missing marketing name."""
    mi300x = passing_amd_runner(sysfs=SYSFS_1.replace('device=0x74b9', 'device=0x74a1'))
    verdict = run_full_check(mi300x, allowlist, proof, pinned_uuids=None, config=CONFIG, now=1_000.0)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_SPEC]
    assert (
        str(check(verdict, ck.GPU_SPEC).evidence['reason']) == 'model 0x74a1 (MI300X) is listed but not qualified yet'
    )
    assert why_of(verdict, ck.GPU_SPEC) == w.render({'code': w.SPEC_MODEL, 'n': 1})
    assert check(verdict, ck.GPU_PROOF).skipped and proof.staged == []


def test_a_partitioned_card_a_zero_serial_and_an_old_stack_each_bench_with_their_phrase(proof, allowlist):
    def verdict_for(sysfs: str) -> CheckVerdict:
        return run_full_check(passing_amd_runner(sysfs=sysfs), allowlist, proof, pinned_uuids=None, config=CONFIG_AMD)

    cpx = verdict_for(SYSFS_1.replace('=SPX', '=CPX').replace('=NPS1', '=NPS4'))
    assert cpx.failed == [ck.GPU_SPEC] and 'partitioned CPX/NPS4' in str(check(cpx, ck.GPU_SPEC).evidence['reason'])
    assert why_of(cpx, ck.GPU_SPEC) == 'the pool admits whole cards only, and this box has 1 partitioned card'
    zero = verdict_for(SYSFS_1.replace('unique_id=675bce773a2403eb', 'unique_id=0000000000000000').replace(f'unique_id {int(AMD_UUIDS[0][4:], 16)}', 'unique_id 0'))  # fmt: skip
    assert zero.failed == [ck.GPU_SPEC] and why_of(zero, ck.GPU_SPEC) == 'this box did not report a usable serial on 1 card'  # fmt: skip
    gone = verdict_for(SYSFS_1.replace('unique_id=675bce773a2403eb\n', 'unique_id=\n'))
    assert gone.failed == [ck.GPU_SPEC] and 'no usable serial' in str(check(gone, ck.GPU_SPEC).evidence['reason'])
    wrong_gfx = verdict_for(SYSFS_1.replace('gfx_target_version 90402', 'gfx_target_version 90010'))
    assert wrong_gfx.failed == [ck.GPU_SPEC] and why_of(wrong_gfx, ck.GPU_SPEC) == w.render({'code': w.SPEC_COMPUTE_CAP, 'n': 1})  # fmt: skip
    # an old kernel AND no DKMS driver (the droplet's 6.19 DKMS would pass the floor on any kernel)
    old = verdict_for(SYSFS_1.replace('kernel=6.8.0-137-generic', 'kernel=5.15.0-122-generic').replace('amdgpu=6.19.14.31400000', 'amdgpu='))  # fmt: skip
    assert old.failed == [ck.AMD_STACK] and why_of(old, ck.AMD_STACK) == (
        "this box's kernel and AMD driver are older than the pool floor (kernel 6.8 or amdgpu DKMS 6.2)"
    )
    assert '5.15.0-122-generic' not in why_of(old, ck.AMD_STACK)  # the version is the box's and stays in the log
    dkms = verdict_for(
        SYSFS_1.replace('kernel=6.8.0-137-generic', 'kernel=5.15.0-122-generic')
    )  # the real DKMS line carries it
    assert dkms.verdict == ADMIT and check(dkms, ck.AMD_STACK).evidence['passed_on'] == 'dkms'
    unreadable = verdict_for('')
    assert unreadable.failed == [ck.GPU_SPEC, ck.AMD_STACK, ck.POWER_LIMIT]
    assert why_of(unreadable, ck.AMD_STACK) == w.PHRASES[w.STACK_UNREADABLE]
    assert ck.check_amd_stack(None, [], 'ssh: reset').evidence[w.PUBLIC] == {'code': w.STACK_UNREADABLE}


def test_kfd_and_render_node_holders_are_judged_like_nvidia_nodes():
    holders = fixture('amd/device_holders_kfd_desktop.txt')
    parsed = parse_device_holders(holders, AMD_GPU_DEVICE)
    assert parsed[1642].devices == ['/dev/dri/renderD129'] and parsed[3310].devices == ['/dev/kfd', '/dev/dri/renderD129']  # fmt: skip
    result = ck.check_card_free(holders, ours=(), vendor=AMD)
    assert not result.passed and result.evidence[w.PUBLIC] == {'code': w.ANOTHER_CONTAINER, 'n': 2}
    assert ck.check_card_free(holders, ours={'c' * 64}, vendor=AMD).evidence[w.PUBLIC] == {'code': w.DESKTOP_SESSION, 'n': 1}  # fmt: skip
    assert ck.check_card_free(holders, ours=(), vendor=NVIDIA).passed  # the NVIDIA pattern sees no NVIDIA node
    assert ck.check_card_free(NO_DEVICE_HOLDERS, ours=(), vendor=AMD).passed
    assert "-lname /dev/kfd -o -lname '/dev/dri/renderD*'" in AMD_DEVICE_HOLDERS_COMMAND
    # the proof's own refusals: the device, not a container runtime, is what says no on AMD
    public = ck._proof_public([{'passed': False, 'not_run': True, 'reason': 'container never started: Error response from daemon: error gathering device information while adding custom device "/dev/kfd": no such file'}])  # fmt: skip
    assert public == {'code': w.PROOF_RUNTIME_AMD, 'n': 1}
    assert w.render(public) == 'the AMD compute device would not start our GPU proof container (1 card)'


# ---------------------------------------------------------------- the heartbeat --------------------------------------


def test_the_heartbeat_re_reads_the_serial_the_partition_the_node_and_the_kernel():
    uuid = AMD_UUIDS[0]
    box = BoxState('hk', IDLE, pinned_uuids=[uuid], card_name='AMD Instinct MI325X', vendor=AMD)
    box.identity = {'power_limits': {uuid: 1000.0}, 'render_nodes': {uuid: 'renderD129'}, 'amd_stack': {'kernel': '6.8.0-137-generic'}}  # fmt: skip

    def same_card(sysfs: str) -> hb.Answer:
        return hb._same_card(FakeRunner({a.AMD_SYSFS_COMMAND: sysfs}), box)

    ok = same_card(SYSFS_1)
    assert ok.ok and ok.detail == '1 pinned present, power + partition + kernel unchanged'
    assert ok.evidence['partition'] == {uuid: 'SPX/NPS1'} and ok.evidence['render_nodes'] == {uuid: 'renderD129'}
    assert 'partitioned CPX/NPS4' in same_card(SYSFS_1.replace('=SPX', '=CPX').replace('=NPS1', '=NPS4')).detail
    assert 'on renderD130, was renderD129' in same_card(SYSFS_1.replace('renderD129', 'renderD130').replace('drm_render_minor 129', 'drm_render_minor 130')).detail  # fmt: skip
    assert 'power cap 600.0 W, was 1000.0 W' in same_card(SYSFS_1.replace('power1_cap=1000000000', 'power1_cap=600000000')).detail  # fmt: skip
    assert 'kernel 6.11.0-9-generic, was 6.8.0-137-generic' in same_card(SYSFS_1.replace('kernel=6.8.0-137-generic', 'kernel=6.11.0-9-generic')).detail  # fmt: skip
    assert 'pinned card(s) missing' in same_card(SYSFS_1.replace('675bce773a2403eb', '675bce773a2403ec')).detail
    assert not same_card('').ok  # the sysfs pass answered nothing: the pinned card is missing
    nvidia_smi_not_asked = FakeRunner({a.AMD_SYSFS_COMMAND: SYSFS_1})
    hb._same_card(nvidia_smi_not_asked, box)
    assert nvidia_smi_not_asked.calls == [a.AMD_SYSFS_COMMAND]
    # the exclusivity question is asked of the device nodes, box-wide, and nvidia-smi never
    record = InstanceRecord(id='i1', entry='e', box=HK, uuid=uuid, container_id='c' * 64)
    alone = hb._alone(
        FakeRunner({AMD_DEVICE_HOLDERS_COMMAND: fixture('amd/device_holders_kfd_desktop.txt')}), [record], AMD
    )
    assert not alone[uuid].ok and '(gnome-shell)' in alone[uuid].detail and 'pid 3310' not in alone[uuid].detail
    free = hb._alone(FakeRunner({AMD_DEVICE_HOLDERS_COMMAND: NO_DEVICE_HOLDERS}), [record], AMD)
    assert free[uuid].ok and free[uuid].detail == '0 device holder(s), none foreign'


# ---------------------------------------------------------------- the rows -------------------------------------------


def test_the_amd_rows_are_listed_matched_by_pci_id_and_priced():
    catalog = load_catalog()
    amd = {t: s for t, s in catalog.items() if s.vendor == AMD}
    assert set(amd) == {'MI300X', 'MI325X', 'MI350', 'MI300A', 'R9700'}
    assert all(s.pci_ids and s.gfx_target and s.compute_cap == s.gfx_target for s in amd.values())
    # MI325X qualified 10/9: the digest reproduced on four runs on the droplet, fill and time limit met (vault 33);
    # MI300X stays listed until Alex calls whether its gfx942 sibling qualifies it
    assert amd['MI325X'].qualified and all(s.status == LISTED for t, s in amd.items() if t != 'MI325X')
    ids = [i for s in amd.values() for i in s.pci_ids]
    assert len(ids) == len(set(ids)) and spec_for_pci_id('0x74A1') is amd['MI300X'] and spec_for_pci_id('') is None
    # the droplet's MI325X reports device 0x74b9 (0x74a5, AMD's listed id, is its subsystem id): both find the row
    assert spec_for_pci_id('0x74b9') is amd['MI325X'] and spec_for_pci_id('0x74a5') is amd['MI325X']
    assert amd['MI325X'].vram_total_mib_min <= 261_824 <= amd['MI325X'].vram_total_mib_max  # the real card's total
    assert amd['MI300X'].counts == (1, 8) and amd['MI350'].names == ('AMD Instinct MI350X', 'AMD Instinct MI355X')
    assert 176_947 <= amd['MI300X'].vram_total_mib_min <= 196_608 <= amd['MI300X'].vram_total_mib_max
    rates = load_rates()
    assert all(t in rates for t in amd) and all(rates[t].target_fleet % 8 == 0 for t in ('MI300X', 'MI325X', 'MI350'))


def test_the_fleet_page_shows_the_vendor_and_the_gfx_target(tmp_path):
    amd_box = rentable_box(vendor=AMD, card_name='AMD Instinct MI325X', cards=AMD_UUIDS[:1])
    row = build_fleet(tmp_path, {HK: amd_box}, {}, {}, False, NOW)['boxes'][0]
    assert row['vendor'] == AMD and row['gfx_target'] == 'gfx942' and row['gpu_type'] == 'MI325X'
    nvidia = build_fleet(tmp_path, {HK: rentable_box()}, {}, {}, False, NOW)['boxes'][0]
    assert nvidia['vendor'] == NVIDIA and nvidia['gfx_target'] is None
