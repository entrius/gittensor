# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The host around the cards (host specs, 10/9): the scrape steps and their parsers (RAM, CPU, disk size, the
download and upload probes, the round trip, the interconnect on both vendors, the CPU model), the ``host_spec`` check
in its advertised and its hard mode, the bandwidth EMAs and the consecutive-round rule, the port floor, the record
on the box, where a box is (the geo cache), its uptime and deploy time, and what ``fleet.json`` publishes per box
and per offer row."""

import json
from unittest.mock import patch

import pytest

from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks import why as w
from gittensor.controller.checks.full_check import FullCheckConfig, run_full_check
from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.checks.scrape import (
    AMD_TOPO_COMMAND,
    CPU_MODEL_COMMAND,
    CPU_THREADS_COMMAND,
    DOWNLOAD_PROBE_COMMAND,
    MEMINFO_COMMAND,
    NVIDIA_TOPO_COMMAND,
    NVLINK,
    PCIE,
    RTT_COMMAND,
    SINGLE,
    UPLOAD_PROBE_COMMAND,
    XGMI,
    DownloadProbe,
    GpuInfo,
    HostScrape,
    Interconnect,
    UploadProbe,
    disk_free_command,
    download_probe_command,
    parse_amd_topo,
    parse_cpu_model,
    parse_cpu_threads,
    parse_df_total_gb,
    parse_download_probe,
    parse_meminfo_total_gb,
    parse_nvidia_topo,
    scrape_host,
)
from gittensor.controller.checks.state import BENCHED, IDLE, BoxState, CardState, apply_verdict, download_due
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.publish import (
    available_host,
    build_fleet,
    deploy_seconds,
    guaranteed_host,
    observed_min_host,
)
from tests.controller.conftest import CONFIG, NETWORK_TARGETS, fixture, passing_amd_runner, passing_runner
from tests.controller.test_publish import HK_A, HK_B, NOW, UUID_A, UUID_B, fleet

# ---------------------------------------------------------------- scrape ---------------------------------------------


def test_the_parsers_read_the_recorded_host():
    assert parse_meminfo_total_gb(fixture('proc_meminfo.txt')) == pytest.approx(131913220 * 1024 / 1e9)
    assert parse_meminfo_total_gb('MemFree: 1 kB\n') is None and parse_meminfo_total_gb('') is None
    assert parse_cpu_threads(fixture('nproc.txt')) == 32 and parse_cpu_threads('0\n') is None
    assert parse_cpu_threads('nproc: not found\n') is None
    assert parse_df_total_gb(fixture('df_docker.txt')) == pytest.approx(1921724676 * 1024 / 1e9)
    assert parse_df_total_gb('') is None and parse_df_total_gb('Filesystem\n/dev/sda1 x y z\n') is None
    probe = parse_download_probe(fixture('curl_download_probe.txt'))
    assert probe == DownloadProbe(200, 29754290, 28412482.0) and probe.mbps == pytest.approx(227.3, abs=0.1)
    # anything short of a 200 with bytes on the wire is no sample, never a number
    assert parse_download_probe('0.000 0 000').mbps is None  # curl could not connect
    assert parse_download_probe('1200000.000 1800000 200').mbps == pytest.approx(9.6)  # cut off at the timeout
    assert parse_download_probe('5000000 29754290 401').mbps is None  # Hub refused the token
    assert parse_download_probe('').mbps is None and parse_download_probe('garbage').mbps is None
    assert parse_cpu_model(fixture('cpu_model.txt')) == 'AMD EPYC 7763 64-Core Processor'
    assert parse_cpu_model('  Intel(R)   Xeon(R)  \n') == 'Intel(R) Xeon(R)' and parse_cpu_model('') == ''
    assert len(parse_cpu_model('x' * 200)) == 64


def test_the_download_probe_pulls_a_pinned_layer_of_the_proof_image_the_way_docker_would():
    cmd = download_probe_command()
    assert cmd == DOWNLOAD_PROBE_COMMAND
    assert f'repository:{cfg.DOWNLOAD_PROBE_REPO}:pull' in cmd  # an anonymous pull token first ...
    assert f'/v2/{cfg.DOWNLOAD_PROBE_REPO}/blobs/{cfg.DOWNLOAD_PROBE_BLOB}' in cmd  # ... then the blob, via the CDN
    assert '-L' in cmd and f'-m {int(cfg.DOWNLOAD_PROBE_TIMEOUT_S)}' in cmd and '-o /dev/null' in cmd
    assert cmd.endswith('|| [ $? -eq 28 ]')  # a pull the timeout cut short still reports its partial count
    assert "'%{speed_download} %{size_download} %{http_code}'" in cmd
    assert 'exit 3' in cmd  # no token: the step fails, which is no sample
    assert 20e6 <= cfg.DOWNLOAD_PROBE_BYTES <= 50e6  # big enough to measure, small enough to repeat every round


def test_the_scrape_adds_the_host_steps_and_a_failed_one_leaves_its_field_none():
    scrape = scrape_host(passing_runner(), network_targets=NETWORK_TARGETS)
    assert scrape.errors == {}
    assert scrape.ram_total_gb == pytest.approx(135.08, abs=0.01) and scrape.cpu_threads == 32
    assert scrape.disk_total_gb == pytest.approx(1967.85, abs=0.01)
    assert scrape.disk_free_gb == pytest.approx(1343.1, abs=0.1)
    assert scrape.down_mbps == pytest.approx(227.3, abs=0.1) and scrape.down_probe is not None
    assert scrape.cpu_model == 'AMD EPYC 7763 64-Core Processor' and scrape.interconnect == Interconnect(SINGLE, 'X')
    # the disk size comes from the one df call the free check makes: no second df
    assert sum(c.startswith('df ') for c in passing_runner_calls(scrape_host, network_targets=())) == 1
    runner = passing_runner().on(MEMINFO_COMMAND, CommandResult(1, '', 'cat: /proc/1/root/proc/meminfo: No such file'))
    runner.on(CPU_THREADS_COMMAND, 'nproc: invalid option\n')
    runner.on(DOWNLOAD_PROBE_COMMAND, CommandResult(3, '', 'no pull token'))
    runner.on(NVIDIA_TOPO_COMMAND, CommandResult(127, '', 'not found')).on(CPU_MODEL_COMMAND, ConnectionError('x'))
    scrape = scrape_host(runner, network_targets=())
    assert scrape.ram_total_gb is None and 'meminfo' in scrape.errors
    assert scrape.cpu_threads is None and 'cpu_threads' not in scrape.errors  # the command answered, unparseably
    assert scrape.down_mbps is None and 'no pull token' in scrape.errors['download']
    assert scrape.down_probe == DownloadProbe()  # ran, nothing came back
    assert scrape.interconnect is None and 'topo' in scrape.errors
    assert scrape.cpu_model == '' and 'cpu_model' in scrape.errors
    # the probes are one real transfer each way per box per round: the caller can leave them out of a visit
    scrape = scrape_host(passing_runner(), network_targets=(), download=False)
    assert scrape.down_probe is None and scrape.down_mbps is None and scrape.ram_total_gb is not None
    assert scrape.up_probe is None and scrape.up_mbps is None and scrape.rtt_ms is not None


def passing_runner_calls(fn, **kw):
    runner = passing_runner()
    fn(runner, **kw)
    return runner.calls


class Clock:
    """A clock the scrape reads: every read advances by the next step given, so a fake run has a duration."""

    def __init__(self, *steps: float):
        self.t, self.steps = 0.0, list(steps)

    def __call__(self) -> float:
        t = self.t
        self.t += self.steps.pop(0) if self.steps else 0.0
        return t


def test_the_upload_is_timed_on_our_clock_less_the_round_trip_and_the_rtt_is_evidence():
    """The box streams 20 MB to us over the session we hold; the seconds it took, less what one empty command costs
    (the round trip), is the wire time. No third party is involved and nothing is measured by the box itself."""
    # the clock reads: rtt start, rtt end (+0.05 s), upload start, upload end (+1.73 s): 20 MB in 1.68 s on the wire
    scrape = scrape_host(passing_runner(), network_targets=(), clock=Clock(0.05, 0.0, 1.73))
    assert scrape.rtt_ms == 50.0 and scrape.up_probe is not None and scrape.up_probe.bytes == cfg.UPLOAD_PROBE_BYTES
    assert scrape.up_mbps == pytest.approx(cfg.UPLOAD_PROBE_BYTES * 8 / 1e6 / 1.68, rel=1e-3)
    assert UPLOAD_PROBE_COMMAND == f"head -c {cfg.UPLOAD_PROBE_BYTES} /dev/zero | tr '\\0' a"
    assert RTT_COMMAND == 'true'
    # a short read (the session died mid-stream) or no measurable time is no sample
    runner = passing_runner().on(UPLOAD_PROBE_COMMAND, 'a' * 1000)
    short = scrape_host(runner, network_targets=(), clock=Clock(0.05, 0.0, 1.0))
    assert short.up_mbps is None and short.up_probe is not None and short.up_probe.bytes == 1000
    assert UploadProbe(cfg.UPLOAD_PROBE_BYTES, 0.0).mbps is None
    assert UploadProbe(cfg.UPLOAD_PROBE_BYTES, 0.2, rtt_s=0.3).mbps is None  # the rtt ate the whole timing
    runner = passing_runner().on(UPLOAD_PROBE_COMMAND, CommandResult(1, '', 'head: error'))
    failed = scrape_host(runner, network_targets=())
    assert failed.up_mbps is None and 'upload' in failed.errors and failed.up_probe == UploadProbe()
    # the rtt step failing leaves the upload timed on its own
    runner = passing_runner().on(RTT_COMMAND, ConnectionError('reset'))
    no_rtt = scrape_host(runner, network_targets=(), clock=Clock(0.0, 0.0, 2.0))
    assert no_rtt.rtt_ms is None and no_rtt.up_probe is not None and no_rtt.up_probe.rtt_s is None
    assert no_rtt.up_mbps == pytest.approx(cfg.UPLOAD_PROBE_BYTES * 8 / 1e6 / 2.0)


def test_the_interconnect_parsers_name_the_worst_link_between_any_two_cards():
    assert parse_nvidia_topo(fixture('nvidia_smi_topo_2x5090.txt')) == Interconnect(PCIE, 'PHB')
    assert parse_nvidia_topo(fixture('nvidia_smi_topo_4xh100.txt')) == Interconnect(NVLINK, 'NV18')  # NICs skipped
    assert parse_nvidia_topo(fixture('nvidia_smi_topo_1x5090.txt')) == Interconnect(SINGLE, 'X')
    mixed = fixture('nvidia_smi_topo_4xh100.txt').replace('GPU3\tNV18\tNV18\tNV18', 'GPU3\tSYS\tNV18\tNV18', 1)
    assert parse_nvidia_topo(mixed) == Interconnect(PCIE, 'SYS')  # one pair across sockets: the box is PCIe-class
    assert parse_nvidia_topo('') == Interconnect() and parse_nvidia_topo('nvidia-smi: not found') == Interconnect()
    assert parse_amd_topo(fixture('amd/kfd_io_links_8.txt')) == Interconnect(XGMI, 'XGMI')
    assert parse_amd_topo(fixture('amd/kfd_io_links_2_pcie.txt')) == Interconnect(PCIE, 'PCIE')
    assert parse_amd_topo(fixture('amd/kfd_io_links_1.txt')) == Interconnect(SINGLE, 'X')
    assert parse_amd_topo('') == Interconnect() and parse_amd_topo('== 0 render=0\n') == Interconnect()
    scrape = scrape_host(passing_runner(topo=fixture('nvidia_smi_topo_2x5090.txt')), network_targets=())
    assert scrape.interconnect == Interconnect(PCIE, 'PHB')
    amd = scrape_host(passing_amd_runner(topo=fixture('amd/kfd_io_links_8.txt')), network_targets=())
    assert amd.interconnect == Interconnect(XGMI, 'XGMI') and amd.errors == {}
    assert '/dev/kfd' not in AMD_TOPO_COMMAND and 'rocm-smi' not in AMD_TOPO_COMMAND  # sysfs only (30 §3)
    assert NVIDIA_TOPO_COMMAND == 'nvidia-smi topo -m' and CPU_MODEL_COMMAND.endswith('| head -n 1')


def test_the_host_steps_are_vendor_neutral():
    """The same host commands, unchanged, on the AMD path (30 §3: the NVIDIA path is never refactored, the AMD path
    runs the same host plumbing); only the topology has a vendor sibling behind the switch."""
    scrape = scrape_host(passing_amd_runner(), network_targets=NETWORK_TARGETS)
    assert scrape.errors == {} and scrape.ram_total_gb and scrape.cpu_threads == 32 and scrape.down_mbps
    assert scrape.up_mbps is not None or scrape.up_probe is not None
    assert scrape.cpu_model and scrape.interconnect == Interconnect(SINGLE, 'X')
    nvidia = passing_runner_calls(scrape_host, network_targets=())
    for command in (MEMINFO_COMMAND, CPU_THREADS_COMMAND, CPU_MODEL_COMMAND, DOWNLOAD_PROBE_COMMAND):
        assert command in nvidia
    for command in (UPLOAD_PROBE_COMMAND, RTT_COMMAND, disk_free_command(), NVIDIA_TOPO_COMMAND):
        assert command in nvidia
    assert AMD_TOPO_COMMAND not in nvidia


# ---------------------------------------------------------------- the check ------------------------------------------


def card(limit=575.0, default=575.0, mib=32607) -> GpuInfo:
    return GpuInfo('GPU-x', 'NVIDIA GeForce RTX 5090', '580.65.06', mib, limit, default, 600.0, '0', '12.0')


def host_scrape(
    ram=135.0,
    cpu=32,
    disk=1967.0,
    down: float | None = 227.0,
    up: float | None = 180.0,
    gpus=1,
    ports=(31000, 31099),
    cards=None,
):
    scrape = HostScrape(
        ram_total_gb=ram,
        cpu_threads=cpu,
        cpu_model='AMD EPYC 7763 64-Core Processor',
        disk_total_gb=disk,
        disk_free_gb=1343.1,
        down_mbps=down,
        up_mbps=up,
        rtt_ms=42.0,
        rent_ports=list(ports),
        gpus=cards if cards is not None else [card() for _ in range(gpus)],
        interconnect=Interconnect(SINGLE, 'X') if gpus == 1 else Interconnect(PCIE, 'PHB'),
    )
    scrape.down_probe = (
        DownloadProbe(200, cfg.DOWNLOAD_PROBE_BYTES, down * 1e6 / 8) if down is not None else DownloadProbe()
    )
    scrape.up_probe = UploadProbe(cfg.UPLOAD_PROBE_BYTES, 1.0) if up is not None else UploadProbe()
    return scrape


def test_the_floors_scale_per_card_and_a_good_box_passes_with_its_record():
    result = ck.check_host_spec(host_scrape(), cfg.RTX_5090, 1, None)
    assert result.passed and result.evidence['host']['shortfalls'] == []
    assert result.evidence['floors'] == {
        'ram_gb': 16.0, 'cpu_threads': 4.0, 'disk_total_gb': 51.9, 'down_mbps': 100.0, 'up_mbps': 50.0, 'port_count': 100.0,
    }  # fmt: skip
    two = ck.check_host_spec(host_scrape(gpus=2), cfg.RTX_5090, 2, None)
    assert two.evidence['floors']['ram_gb'] == 32.0 and two.evidence['floors']['cpu_threads'] == 8.0
    assert two.evidence['floors']['disk_total_gb'] == 103.8  # 1.5 x 2 x 33 000 MiB, our spec's VRAM, never the box's
    host = result.evidence['host']
    assert (host['ram_gb'], host['cpu_threads'], host['disk_total_gb']) == (135.0, 32, 1967.0)
    assert host['down_mbps'] == 227.0 == host['down_sample_mbps'] and host['down_below_rounds'] == 0
    assert host['up_mbps'] == 180.0 == host['up_sample_mbps'] and host['up_below_rounds'] == 0
    assert host['down_at'] is not None
    # the rest of the record: what the scrape already knew, published rather than re-measured
    assert host['cpu_model'] == 'AMD EPYC 7763 64-Core Processor' and host['disk_free_gb'] == 1343.1
    assert host['vram_gb'] == 34.2 and host['rtt_ms'] == 42.0 and host['port_count'] == 100
    assert host['interconnect'] == SINGLE and host['interconnect_raw'] == 'X'
    assert host['power_w'] == 575.0 and host['power_limited'] is False
    lowered = ck.check_host_spec(host_scrape(cards=[card(), card(limit=500.0)], gpus=2), cfg.RTX_5090, 2, None)
    assert lowered.evidence['host']['power_w'] == 575.0 and lowered.evidence['host']['power_limited'] is True
    assert lowered.evidence['host']['vram_gb'] == 68.4 and lowered.evidence['host']['interconnect'] == PCIE
    idle_only = ck.check_host_spec(host_scrape(ports=()), cfg.RTX_5090, 1, None)
    assert idle_only.passed and idle_only.evidence['host']['port_count'] is None  # no range offered: not judged


def test_a_narrow_rent_range_is_refused_and_a_dev_box_keeps_its_dev_minimum():
    """The port floor is hard from the start (`gitt up --rent` already refuses a range under RENT_PORTS_MIN): a box
    whose agent carries a narrower range fails, through the normal path. A dev box (a Lium pod with a handful of
    mapped ports, admitted by image ID) is held to the dev minimum the CLI sets on the config instead."""
    narrow = ck.check_host_spec(host_scrape(ports=(31096, 31099)), cfg.RTX_5090, 1, None)
    assert not narrow.passed and narrow.evidence[w.PUBLIC] == {'code': w.PORTS_BELOW_FLOOR, 'n': 4, 'floor': 100}
    phrase = w.render(narrow.evidence[w.PUBLIC])
    assert phrase.startswith('the rent port range is 4 ports wide, and a rentable box opens at least 100')
    assert narrow.evidence['host']['shortfalls'] == [w.PORTS_BELOW_FLOOR]
    assert ck.check_host_spec(host_scrape(ports=(31096, 31099)), cfg.RTX_5090, 1, None, ports_min=4).passed
    assert FullCheckConfig().ports_min == cfg.PORTS_MIN == 100


def test_advertised_mode_passes_and_lists_every_shortfall_with_its_number_and_the_floor():
    result = ck.check_host_spec(host_scrape(ram=16, cpu=4, disk=40), cfg.RTX_5090, 2, None, hard=False)
    assert result.passed
    assert result.evidence['host']['shortfalls'] == [w.RAM_BELOW_FLOOR, w.CPU_BELOW_FLOOR, w.DISK_RATIO_BELOW_FLOOR]
    assert result.evidence['reason'] == 'RAM 16 GB < 32 GB; CPU threads 4 < 8; disk 40 GB < 104 GB (1.5x 69 GB VRAM)'
    assert w.PUBLIC not in result.evidence  # nothing to bench, nothing to render
    # a field the scrape could not read is no shortfall and no reading
    unread = ck.check_host_spec(HostScrape(down_mbps=200.0), cfg.RTX_5090, 1, None, hard=False)
    assert unread.passed and unread.evidence['unreadable'] == ['meminfo', 'cpu_threads', 'disk_free']
    assert unread.evidence['host']['shortfalls'] == [] and unread.evidence['host']['ram_gb'] is None
    # with no catalog row (an unknown card) the disk ratio has no floor and the per-card floors still apply
    nospec = ck.check_host_spec(host_scrape(disk=10), None, 1, None, hard=False)
    assert nospec.passed and nospec.evidence['floors']['disk_total_gb'] is None
    assert nospec.evidence['host']['shortfalls'] == []


def test_hard_mode_fails_a_shortfall_with_a_public_phrase_that_names_the_fix():
    result = ck.check_host_spec(host_scrape(ram=16), cfg.RTX_5090, 2, None, hard=True)
    assert not result.passed
    assert result.evidence[w.PUBLIC] == {'code': w.RAM_BELOW_FLOOR, 'ram_gb': 16, 'count': 2, 'floor_gb': 32}
    assert w.render(result.evidence[w.PUBLIC]) == 'host RAM is 16 GB, and a 2-card box of this type needs at least 32 GB'  # fmt: skip
    cpu = ck.check_host_spec(host_scrape(cpu=2), cfg.RTX_5090, 1, None, hard=True)
    assert w.render(cpu.evidence[w.PUBLIC]) == 'the host has 2 CPU threads, and a 1-card box of this type needs at least 4 threads'  # fmt: skip
    disk = ck.check_host_spec(host_scrape(disk=40), cfg.RTX_5090, 1, None, hard=True)
    assert w.render(disk.evidence[w.PUBLIC]) == "total disk is 40 GB, and idle pay needs 1.5x the cards' VRAM, 51 GB"
    unread = ck.check_host_spec(HostScrape(down_mbps=200.0), cfg.RTX_5090, 1, None, hard=True)
    assert not unread.passed and unread.evidence[w.PUBLIC] == {'code': w.HOST_UNREADABLE}  # fails closed
    assert ck.check_host_spec(host_scrape(), cfg.RTX_5090, 1, None, hard=True).passed
    assert ck.check_host_spec(host_scrape(ram=16), cfg.RTX_5090, 2, None).passed  # advertised today
    with patch.object(cfg, 'HOST_SPEC_HARD', True):  # read when the check runs, not when the module loaded
        assert not ck.check_host_spec(host_scrape(ram=16), cfg.RTX_5090, 2, None).passed


def rounds(samples, history=None, hard=False, direction='down'):
    """Run the check round after round, feeding each round the record the last one wrote, as the controller does."""
    out = []
    for sample in samples:
        scrape = host_scrape(down=sample) if direction == 'down' else host_scrape(up=sample)
        result = ck.check_host_spec(scrape, cfg.RTX_5090, 1, history, hard=hard)
        history = result.evidence['host']
        out.append(result)
    return out


def test_the_download_ema_runs_across_rounds_and_a_miss_is_no_sample():
    first, second, third = rounds([200.0, 100.0, None])
    assert first.evidence['host']['down_mbps'] == 200.0  # the first sample is the EMA
    assert second.evidence['host']['down_mbps'] == 170.0  # 0.3 x 100 + 0.7 x 200
    assert third.evidence['host']['down_mbps'] == 170.0 and third.evidence['host']['down_sample_mbps'] is None
    assert third.passed and third.evidence['host']['shortfalls'] == []
    assert second.evidence['host']['down_at'] is not None


def test_the_download_floor_fails_only_after_three_sampled_rounds_under_it():
    """9/19 must not repeat: one slow or missed probe never benches a box. The failure needs BANDWIDTH_FAIL_AFTER
    sampled rounds in a row with the EMA under the floor; a miss in between neither counts nor resets."""
    results = rounds([50.0, None, 50.0, None, 50.0])
    assert [r.passed for r in results] == [True, True, True, True, False]
    assert [r.evidence['host']['down_below_rounds'] for r in results] == [1, 1, 2, 2, 3]
    assert results[0].evidence['host']['shortfalls'] == [w.DOWNLOAD_BELOW_FLOOR]  # advertised from the first round
    failed = results[-1]
    assert failed.evidence[w.PUBLIC] == {'code': w.DOWNLOAD_BELOW_FLOOR, 'mbps': 50, 'floor': 100}
    assert w.render(failed.evidence[w.PUBLIC]) == 'download measured 50 Mbps over the last 3 rounds, and the floor is 100 Mbps'  # fmt: skip
    # a sampled round that lifts the EMA back over the floor starts the count over, and the EMA (not the sample) is
    # what is judged from then on: 0.3 x 400 + 0.7 x 50 = 155, then 123, then 101, all over the floor
    recovered = rounds([50.0, 50.0, 400.0, 50.0, 50.0])
    assert [r.passed for r in recovered] == [True] * 5
    assert [r.evidence['host']['down_below_rounds'] for r in recovered] == [1, 2, 0, 0, 0]
    assert [round(r.evidence['host']['down_mbps']) for r in recovered] == [50, 50, 155, 124, 101]
    # the EMA, not the sample, is what is judged: one bad sample after good rounds is under no floor
    assert rounds([300.0, 300.0, 20.0])[-1].evidence['host']['down_below_rounds'] == 0
    # download fails in advertised mode too: it and upload are the hard floors at launch
    assert not rounds([50.0] * cfg.BANDWIDTH_FAIL_AFTER, hard=False)[-1].passed


def test_upload_has_its_own_ema_and_count_under_the_same_rules():
    results = rounds([20.0, None, 20.0, 20.0], direction='up')
    assert [r.passed for r in results] == [True, True, True, False]
    assert [r.evidence['host']['up_below_rounds'] for r in results] == [1, 1, 2, 3]
    assert [r.evidence['host']['down_below_rounds'] for r in results] == [0, 0, 0, 0]  # the directions never mix
    failed = results[-1]
    assert failed.evidence[w.PUBLIC] == {'code': w.UPLOAD_BELOW_FLOOR, 'mbps': 20, 'floor': 50}
    assert w.render(failed.evidence[w.PUBLIC]) == 'upload measured 20 Mbps over the last 3 rounds, and the floor is 50 Mbps'  # fmt: skip
    assert results[0].evidence['host']['shortfalls'] == [w.UPLOAD_BELOW_FLOOR]
    assert rounds([20.0, 20.0, 300.0], direction='up')[-1].evidence['host']['up_below_rounds'] == 0
    # a round where only the upload sampled still stamps the box as probed
    only_up = ck.check_host_spec(host_scrape(down=None, up=150.0), cfg.RTX_5090, 1, {'down_mbps': 200.0})
    assert only_up.evidence['host']['down_mbps'] == 200.0 and only_up.evidence['host']['up_mbps'] == 150.0
    assert only_up.evidence['host']['down_at'] is not None


# ---------------------------------------------------------------- the box record -------------------------------------


def verdict_with(host: dict, passed: bool = True) -> CheckVerdict:
    checks = [CheckResult('gpu_spec', True), CheckResult(ck.HOST_SPEC, passed, {'host': host})]
    return CheckVerdict.from_checks(checks, [UUID_A], 'NVIDIA GeForce RTX 5090', '580.65.06', now=NOW)


def test_every_verdict_keeps_the_record_on_the_box_and_a_bench_restarts_the_counts():
    box = BoxState(HK_A)
    assert download_due(box, NOW)  # never sampled
    first = {
        'ram_gb': 135.0, 'down_mbps': 50.0, 'down_at': NOW - 100, 'down_below_rounds': 2, 'up_below_rounds': 1,
        'shortfalls': [w.DOWNLOAD_BELOW_FLOOR],
    }  # fmt: skip
    box = apply_verdict(box, verdict_with(first), NOW)
    assert box.status == IDLE and box.host_specs == first
    assert not download_due(box, NOW) and download_due(box, NOW + cfg.FULL_CHECK_INTERVAL_S)
    failing = verdict_with({**first, 'down_below_rounds': 3, 'up_below_rounds': 2}, passed=False)
    benched = apply_verdict(box, failing, NOW + 1)
    assert benched.status == BENCHED and benched.last_failed == [ck.HOST_SPEC]
    assert benched.host_specs['down_mbps'] == 50.0  # history stays ...
    assert (
        benched.host_specs['down_below_rounds'] == 0 and benched.host_specs['up_below_rounds'] == 0
    )  # ... the counts restart
    # a strike (nothing judged) still moves the record on: the EMA is measurement, not judgement
    not_run = CheckVerdict.from_checks(
        [
            CheckResult(ck.HOST_SPEC, True, {'host': {**first, 'down_mbps': 80.0}}),
            CheckResult('gpu_proof', False, {}, not_run=True),
        ],
        [UUID_A],
        now=NOW,
    )
    idle = BoxState(HK_A, status=IDLE, pinned_uuids=[UUID_A], cards={UUID_A: CardState()})
    struck = apply_verdict(idle, not_run, NOW)
    assert struck.not_run_count == 1 and struck.host_specs['down_mbps'] == 80.0
    # a verdict without the check (an older controller's) leaves what the box had
    bare = CheckVerdict.from_checks([CheckResult('gpu_spec', True)], [UUID_A], now=NOW)
    assert apply_verdict(box, bare, NOW).host_specs == first
    box.location = {'country': 'US', 'region': 'Texas', 'city': 'Dallas', 'at': NOW, 'ip': '203.0.113.7'}
    assert BoxState.from_dict(box.as_dict()).host_specs == first  # round-trips through the store
    assert BoxState.from_dict(box.as_dict()).location == box.location


def test_the_full_check_feeds_the_record_through_and_the_recorded_box_is_admitted(proof, allowlist):
    history = {'down_mbps': 100.0, 'down_below_rounds': 0}
    verdict = run_full_check(passing_runner(), allowlist, proof, config=CONFIG, host_history=history)
    assert verdict.admitted
    host = verdict.host
    assert host['ram_gb'] == 135.1 and host['cpu_threads'] == 32 and host['disk_total_gb'] == 1967.8
    assert host['down_mbps'] == 138.2 and host['down_sample_mbps'] == 227.3  # 0.3 x 227 + 0.7 x 100
    assert host['shortfalls'] == [] and host['up_below_rounds'] == 0
    assert host['cpu_model'] == 'AMD EPYC 7763 64-Core Processor' and host['interconnect'] == SINGLE
    assert host['vram_gb'] == 34.2 and host['power_w'] == 575.0 and host['port_count'] is None  # not --rent
    assert verdict.as_dict()['host'] == host
    # three rounds under the floor on the box's record, and this one still under: the normal failed-check path
    slow = passing_runner(download='1250000 29754290 200')  # 10 Mbps
    history = {'down_mbps': 20.0, 'down_below_rounds': 2}
    verdict = run_full_check(slow, allowlist, proof, config=CONFIG, host_history=history)
    assert verdict.failed == [ck.HOST_SPEC] and verdict.skipped == ['gpu_proof']
    box = apply_verdict(BoxState(HK_A), verdict, NOW)
    assert box.status == BENCHED and box.last_failed_why[ck.HOST_SPEC].startswith('download measured 17 Mbps')


# ---------------------------------------------------------------- published ------------------------------------------


def test_guaranteed_is_the_hard_floors_times_the_size_and_observed_min_the_least_free_box():
    assert guaranteed_host(2, 'RTX5090', hard=False) == {
        'ram_gb': None, 'cpu_threads': None, 'disk_total_gb': None, 'down_mbps': 100.0, 'up_mbps': 50.0, 'port_count': 100,
    }  # fmt: skip
    assert guaranteed_host(4, 'RTX5090', hard=True) == {
        'ram_gb': 64.0, 'cpu_threads': 16, 'disk_total_gb': 207.6, 'down_mbps': 100.0, 'up_mbps': 50.0, 'port_count': 100,
    }  # fmt: skip
    assert guaranteed_host(1, 'NOPE', hard=True)['disk_total_gb'] is None  # no catalog row: no VRAM to scale by
    hosts = [
        {
            'ram_gb': 135.0,
            'cpu_threads': 32,
            'down_mbps': 227.0,
            'up_mbps': 90.0,
            'rtt_ms': 12.0,
            'interconnect': NVLINK,
            'power_w': 700.0,
            'vram_gb': 160.0,
            'disk_free_gb': 900.0,
            'disk_total_gb': 2000.0,
            'port_count': 100,
        },  # noqa: E501
        {
            'ram_gb': 64.0,
            'cpu_threads': 48,
            'down_mbps': None,
            'up_mbps': 60.0,
            'rtt_ms': 80.0,
            'interconnect': PCIE,
            'power_w': 575.0,
            'vram_gb': 64.0,
            'disk_free_gb': 100.0,
            'disk_total_gb': 500.0,
            'port_count': 120,
        },  # noqa: E501
        {'ram_gb': None, 'cpu_threads': 'x', 'down_mbps': 150.0, 'interconnect': 'tin cans'},
    ]
    assert observed_min_host(hosts) == {
        'ram_gb': 64.0, 'cpu_threads': 32.0, 'disk_total_gb': 500.0, 'disk_free_gb': 100.0, 'vram_gb': 64.0,
        'down_mbps': 150.0, 'up_mbps': 60.0, 'port_count': 100.0, 'power_w': 575.0, 'rtt_ms': 80.0, 'interconnect': PCIE,
    }  # fmt: skip
    assert observed_min_host([{'interconnect': NVLINK}, {'interconnect': SINGLE}])['interconnect'] == NVLINK
    assert all(v is None for v in observed_min_host([]).values())
    hosts = [
        {'location': {'country': 'US'}, 'deploy_s': 40.0, 'uptime_30d_pct': 99.0},
        {'location': {'country': 'DE'}, 'deploy_s': 70.0, 'uptime_30d_pct': 80.0},
        {'location': None, 'deploy_s': None, 'uptime_30d_pct': 100.0},
        {'location': {'country': 'US'}, 'deploy_s': 55.0},
    ]
    assert available_host(hosts) == {'countries': ['DE', 'US'], 'deploy_s': 55.0, 'uptime_30d_pct': 99.0}
    assert available_host([]) == {'countries': [], 'deploy_s': None, 'uptime_30d_pct': None}


def test_the_document_carries_the_host_per_box_and_the_three_objects_per_offer_row(tmp_path):
    boxes, instances = fleet()
    a = boxes[HK_A]
    a.rent_ports = [31000, 31099]
    a.cards = {UUID_A: CardState(IDLE, '', NOW), UUID_B: CardState(IDLE, '', NOW)}
    a.host_specs = {
        'ram_gb': 125.4, 'cpu_threads': 32, 'cpu_model': 'AMD EPYC 7763 64-Core Processor', 'disk_total_gb': 1967.8,
        'disk_free_gb': 1343.1, 'vram_gb': 68.4, 'down_mbps': 227.3, 'down_sample_mbps': 230.0, 'down_at': NOW - 100,
        'down_below_rounds': 0, 'up_mbps': 180.0, 'up_below_rounds': 0, 'rtt_ms': 42.0, 'interconnect': PCIE,
        'interconnect_raw': 'PHB', 'power_w': 575.0, 'power_limited': False, 'port_count': 100,
        'shortfalls': [w.CPU_BELOW_FLOOR, 'free text'],
    }  # fmt: skip
    a.location = {'country': 'US', 'region': 'Texas', 'city': 'Dallas', 'at': NOW - 3600, 'ip': '203.0.113.7'}
    a.admitted_at = NOW - 5 * 86_400
    doc = build_fleet(tmp_path, boxes, {}, {}, True, NOW)
    row_a = next(x for x in doc['boxes'] if x['hotkey'] == HK_A)
    assert row_a['host'] == {
        'cpu_threads': 32.0, 'cpu_model': 'AMD EPYC 7763 64-Core Processor', 'ram_gb': 125.4, 'disk_total_gb': 1967.8,
        'disk_free_gb': 1343.1, 'vram_gb': 68.4, 'down_mbps': 227.3, 'up_mbps': 180.0, 'rtt_ms': 42.0,
        'interconnect': PCIE, 'power_w': 575.0, 'power_limited': False, 'port_count': 100.0,
        'location': {'country': 'US', 'region': 'Texas', 'city': 'Dallas'},
        'uptime_30d_pct': 0.0,  # admitted five days ago and no ledger rollup under this root: never seen up
        'admitted_at': NOW - 5 * 86_400, 'deploy_s': None,
        'shortfalls': [w.CPU_BELOW_FLOOR],  # our codes only; the sample and the stamp stay on the box
    }  # fmt: skip
    assert 'down_sample_mbps' not in row_a['host'] and 'free text' not in str(doc) and '203.0.113.7' not in str(doc)
    row_b = next(x for x in doc['boxes'] if x['hotkey'] == HK_B)
    assert row_b['host']['location'] is None and row_b['host']['cpu_model'] is None
    assert row_b['host']['shortfalls'] == [] and row_b['host']['power_limited'] is False
    assert set(row_b['host']) == set(row_a['host']) and row_b['host']['uptime_30d_pct'] is None  # never admitted
    offer = doc['offers']['RTX5090']['2']
    assert offer == {
        'boxes': 1,
        'guaranteed': {'ram_gb': None, 'cpu_threads': None, 'disk_total_gb': None, 'down_mbps': 100.0, 'up_mbps': 50.0, 'port_count': 100},  # noqa: E501
        'observed_min': {
            'ram_gb': 125.4, 'cpu_threads': 32.0, 'disk_total_gb': 1967.8, 'disk_free_gb': 1343.1, 'vram_gb': 68.4,
            'down_mbps': 227.3, 'up_mbps': 180.0, 'port_count': 100.0, 'power_w': 575.0, 'rtt_ms': 42.0, 'interconnect': PCIE,
        },
        'available': {'countries': ['US'], 'deploy_s': None, 'uptime_30d_pct': 0.0},
    }  # fmt: skip
    with patch.object(cfg, 'HOST_SPEC_HARD', True):
        hard = build_fleet(tmp_path, boxes, {}, {}, True, NOW)['offers']['RTX5090']['2']
    assert hard['guaranteed'] == {
        'ram_gb': 32.0,
        'cpu_threads': 8,
        'disk_total_gb': 103.8,
        'down_mbps': 100.0,
        'up_mbps': 50.0,
        'port_count': 100,
    }  # noqa: E501
    assert HK_A not in str(doc['offers'])  # a row names no box
    # strings the box or the geo provider typed are held to a pattern, never copied through
    a.host_specs['cpu_model'] = '<script>alert(1)</script>'
    a.location = {'country': 'us', 'region': 'Texas; drop', 'city': 'Dallas', 'at': NOW}
    row_a = next(x for x in build_fleet(tmp_path, boxes, {}, {}, True, NOW)['boxes'] if x['hotkey'] == HK_A)
    assert row_a['host']['cpu_model'] is None and row_a['host']['location'] is None
    a.location['country'] = 'US'
    row_a = next(x for x in build_fleet(tmp_path, boxes, {}, {}, True, NOW)['boxes'] if x['hotkey'] == HK_A)
    assert row_a['host']['location'] == {'country': 'US', 'region': None, 'city': 'Dallas'}


def test_deploy_time_is_the_median_over_the_last_rentals_that_reached_sshd():
    class R:
        def __init__(self, box, created_at, placed_at, started_at):
            self.box, self.created_at, self.placed_at, self.started_at = box, created_at, placed_at, started_at

    rentals = {
        'a': R(HK_A, 1, 10.0, 50.0),
        'b': R(HK_A, 2, 10.0, 80.0),
        'c': R(HK_A, 3, 10.0, None),  # never came up: no sample
        'd': R(HK_B, 4, 10.0, 20.0),
    }
    assert deploy_seconds(HK_A, rentals) is None  # two samples: fewer than DEPLOY_MIN_N
    rentals['e'] = R(HK_A, 5, 10.0, 100.0)
    assert deploy_seconds(HK_A, rentals) == 70.0
    for i in range(cfg.DEPLOY_SAMPLE_N):  # the window slides: the last N only
        rentals[f'f{i}'] = R(HK_A, 10 + i, 0.0, 30.0)
    assert deploy_seconds(HK_A, rentals) == 30.0 and deploy_seconds(HK_B, rentals) is None


def test_uptime_is_the_share_of_the_window_the_box_accrued_from_the_daily_rollups(tmp_path):
    from gittensor.controller.pay.ledger import DAY_S, box_uptime, utc_day

    ledger = tmp_path / 'ledger'
    ledger.mkdir()
    now = 1_789_000_000.0
    for days_ago, seconds in (
        (0, 3_600.0),
        (1, 86_400.0),
        (2, 43_200.0),
        (40, 86_400.0),
    ):  # day 40 is out of the window
        day = utc_day(now - days_ago * DAY_S)
        doc = {
            'day': day,
            'hotkeys': {
                HK_A: {
                    UUID_A: {'idle_s': seconds, 'leased_s': 0.0, 'withheld_s': 0.0},
                    UUID_B: {'idle_s': 0.0, 'leased_s': seconds / 2, 'withheld_s': 0.0},
                }
            },
        }  # noqa: E501
        (ledger / f'{day}.rollup.json').write_text(json.dumps(doc))
    boxes = {HK_A: BoxState(HK_A, admitted_at=now - 3 * DAY_S), HK_B: BoxState(HK_B)}
    up = box_uptime(ledger, boxes, now)
    # the box was up 1 h + 24 h + 12 h of the 3 days since it was admitted: a card's max per day, not the sum
    assert up[HK_A] == round(100.0 * (3_600 + 86_400 + 43_200) / (3 * DAY_S), 1) and up[HK_B] is None
    # a box the record saw before its (re-)admission counts from the first rollup day in the window
    boxes[HK_A].admitted_at = now - 60
    later = box_uptime(ledger, boxes, now)[HK_A]
    first = up[HK_A]
    assert later is not None and first is not None and later > first > 0.0  # a shorter span, the same seconds
    # admitted longer than the window: the window is the span, and the share is clamped at 100
    boxes[HK_A].admitted_at = now - 90 * DAY_S
    capped = box_uptime(ledger, boxes, now)[HK_A]
    assert capped is not None and 0.0 < capped <= 100.0
    assert box_uptime(tmp_path / 'none', boxes, now) == {
        HK_A: 0.0,
        HK_B: None,
    }  # no ledger yet: admitted, never seen up
