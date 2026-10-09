# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The host around the cards (host specs, 10/9): the three scrape steps and their parsers, the ``host_spec`` check in its
advertised and its hard mode, the download EMA and the consecutive-round rule, the record on the box, and what
``fleet.json`` publishes per box and per offer row."""

from unittest.mock import patch

import pytest

from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks import why as w
from gittensor.controller.checks.full_check import run_full_check
from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.checks.scrape import (
    CPU_THREADS_COMMAND,
    DOWNLOAD_PROBE_COMMAND,
    MEMINFO_COMMAND,
    DownloadProbe,
    HostScrape,
    disk_free_command,
    download_probe_command,
    parse_cpu_threads,
    parse_df_total_gb,
    parse_download_probe,
    parse_meminfo_total_gb,
    scrape_host,
)
from gittensor.controller.checks.state import BENCHED, IDLE, BoxState, CardState, apply_verdict, download_due
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.publish import build_fleet, guaranteed_host, observed_min_host
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
    assert parse_download_probe('1200000.000 1800000 200').mbps == pytest.approx(
        9.6
    )  # cut off at the timeout: the sample
    assert parse_download_probe('5000000 29754290 401').mbps is None  # Hub refused the token
    assert parse_download_probe('').mbps is None and parse_download_probe('garbage').mbps is None


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


def test_the_scrape_adds_three_host_steps_and_a_failed_one_leaves_its_field_none():
    scrape = scrape_host(passing_runner(), network_targets=NETWORK_TARGETS)
    assert scrape.errors == {}
    assert scrape.ram_total_gb == pytest.approx(135.08, abs=0.01) and scrape.cpu_threads == 32
    assert scrape.disk_total_gb == pytest.approx(1967.85, abs=0.01) and scrape.disk_free_gb == pytest.approx(1343.1, abs=0.1)  # fmt: skip
    assert scrape.down_mbps == pytest.approx(227.3, abs=0.1) and scrape.down_probe is not None
    # the disk size comes from the one df call the free check makes: no second df
    assert sum(c.startswith('df ') for c in passing_runner_calls(scrape_host, network_targets=())) == 1
    runner = passing_runner().on(MEMINFO_COMMAND, CommandResult(1, '', 'cat: /proc/1/root/proc/meminfo: No such file'))
    runner.on(CPU_THREADS_COMMAND, 'nproc: invalid option\n').on(
        DOWNLOAD_PROBE_COMMAND, CommandResult(3, '', 'no pull token')
    )
    scrape = scrape_host(runner, network_targets=())
    assert scrape.ram_total_gb is None and 'meminfo' in scrape.errors
    assert scrape.cpu_threads is None and 'cpu_threads' not in scrape.errors  # the command answered, unparseably
    assert scrape.down_mbps is None and 'no pull token' in scrape.errors['download']
    assert scrape.down_probe == DownloadProbe()  # ran, nothing came back
    # the probe is one real transfer per box per round: the caller can leave it out of a visit
    scrape = scrape_host(passing_runner(), network_targets=(), download=False)
    assert scrape.down_probe is None and scrape.down_mbps is None and scrape.ram_total_gb is not None


def passing_runner_calls(fn, **kw):
    runner = passing_runner()
    fn(runner, **kw)
    return runner.calls


def test_the_host_steps_are_vendor_neutral():
    """The same three commands, unchanged, on the AMD path (30 §3: the NVIDIA path is never refactored, the AMD path
    runs the same host plumbing)."""
    scrape = scrape_host(passing_amd_runner(), network_targets=NETWORK_TARGETS)
    assert scrape.errors == {} and scrape.ram_total_gb and scrape.cpu_threads == 32 and scrape.down_mbps
    nvidia = passing_runner_calls(scrape_host, network_targets=())
    for command in (MEMINFO_COMMAND, CPU_THREADS_COMMAND, DOWNLOAD_PROBE_COMMAND, disk_free_command()):
        assert command in nvidia


# ---------------------------------------------------------------- the check ------------------------------------------


def host_scrape(ram=135.0, cpu=32, disk=1967.0, down=227.0, gpus=1):
    scrape = HostScrape(ram_total_gb=ram, cpu_threads=cpu, disk_total_gb=disk, down_mbps=down)
    scrape.down_probe = DownloadProbe(200, cfg.DOWNLOAD_PROBE_BYTES, down * 1e6 / 8) if down is not None else DownloadProbe()  # fmt: skip
    return scrape


def test_the_floors_scale_per_card_and_a_good_box_passes_with_its_record():
    result = ck.check_host_spec(host_scrape(), cfg.RTX_5090, 1, None)
    assert result.passed and result.evidence['host']['shortfalls'] == []
    assert result.evidence['floors'] == {'ram_gb': 16.0, 'cpu_threads': 4.0, 'disk_total_gb': 51.9, 'down_mbps': 100.0}
    two = ck.check_host_spec(host_scrape(gpus=2), cfg.RTX_5090, 2, None)
    assert two.evidence['floors']['ram_gb'] == 32.0 and two.evidence['floors']['cpu_threads'] == 8.0
    assert two.evidence['floors']['disk_total_gb'] == 103.8  # 1.5 x 2 x 33 000 MiB, our spec's VRAM, never the box's
    host = result.evidence['host']
    assert (host['ram_gb'], host['cpu_threads'], host['disk_total_gb']) == (135.0, 32, 1967.0)
    assert host['down_mbps'] == 227.0 == host['down_sample_mbps'] and host['down_below_rounds'] == 0
    assert host['up_mbps'] is None and host['down_at'] is not None


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
    assert nospec.passed and nospec.evidence['floors']['disk_total_gb'] is None and nospec.evidence['host']['shortfalls'] == []  # fmt: skip


def test_hard_mode_fails_a_shortfall_with_a_public_phrase_that_names_the_fix():
    result = ck.check_host_spec(host_scrape(ram=16), cfg.RTX_5090, 2, None, hard=True)
    assert not result.passed and result.evidence[w.PUBLIC] == {'code': w.RAM_BELOW_FLOOR, 'ram_gb': 16, 'count': 2, 'floor_gb': 32}  # fmt: skip
    assert (
        w.render(result.evidence[w.PUBLIC]) == 'host RAM is 16 GB, and a 2-card box of this type needs at least 32 GB'
    )
    cpu = ck.check_host_spec(host_scrape(cpu=2), cfg.RTX_5090, 1, None, hard=True)
    assert w.render(cpu.evidence[w.PUBLIC]) == 'the host has 2 CPU threads, and a 1-card box of this type needs at least 4 threads'  # fmt: skip
    disk = ck.check_host_spec(host_scrape(disk=40), cfg.RTX_5090, 1, None, hard=True)
    assert w.render(disk.evidence[w.PUBLIC]) == "total disk is 40 GB, and idle pay needs 1.5x the cards' VRAM, 51 GB"
    unread = ck.check_host_spec(HostScrape(down_mbps=200.0), cfg.RTX_5090, 1, None, hard=True)
    assert not unread.passed and unread.evidence[w.PUBLIC] == {
        'code': w.HOST_UNREADABLE
    }  # fails closed, like disk_free
    assert ck.check_host_spec(host_scrape(), cfg.RTX_5090, 1, None, hard=True).passed
    # the flip is one constant: the check's default reads it
    assert ck.check_host_spec(host_scrape(ram=16), cfg.RTX_5090, 2, None).passed  # advertised today
    with patch.object(cfg, 'HOST_SPEC_HARD', True):  # read when the check runs, not when the module loaded
        assert not ck.check_host_spec(host_scrape(ram=16), cfg.RTX_5090, 2, None).passed


def rounds(samples, history=None, hard=False):
    """Run the check round after round, feeding each round the record the last one wrote, as the controller does."""
    out = []
    for sample in samples:
        result = ck.check_host_spec(host_scrape(down=sample), cfg.RTX_5090, 1, history, hard=hard)
        history = result.evidence['host']
        out.append(result)
    return out


def test_the_download_ema_runs_across_rounds_and_a_miss_is_no_sample():
    first, second, third = rounds([200.0, 100.0, None])
    assert first.evidence['host']['down_mbps'] == 200.0  # the first sample is the EMA
    assert second.evidence['host']['down_mbps'] == 170.0  # 0.3 x 100 + 0.7 x 200
    assert third.evidence['host']['down_mbps'] == 170.0 and third.evidence['host']['down_sample_mbps'] is None
    assert third.passed and third.evidence['host']['shortfalls'] == []
    assert second.evidence['host']['down_at'] is not None and third.evidence['host']['down_at'] == second.evidence['host']['down_at']  # fmt: skip


def test_the_download_floor_fails_only_after_three_sampled_rounds_under_it():
    """9/19 must not repeat: one slow or missed probe never benches a box. The failure needs DOWNLOAD_FAIL_AFTER
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
    # download fails in advertised mode too: it is the one hard floor at launch
    assert not rounds([50.0] * cfg.DOWNLOAD_FAIL_AFTER, hard=False)[-1].passed


# ---------------------------------------------------------------- the box record -------------------------------------


def verdict_with(host: dict, passed: bool = True) -> CheckVerdict:
    checks = [CheckResult('gpu_spec', True), CheckResult(ck.HOST_SPEC, passed, {'host': host})]
    return CheckVerdict.from_checks(checks, [UUID_A], 'NVIDIA GeForce RTX 5090', '580.65.06', now=NOW)


def test_every_verdict_keeps_the_record_on_the_box_and_a_bench_restarts_the_count():
    box = BoxState(HK_A)
    assert download_due(box, NOW)  # never sampled
    first = {'ram_gb': 135.0, 'down_mbps': 50.0, 'down_at': NOW - 100, 'down_below_rounds': 2, 'shortfalls': [w.DOWNLOAD_BELOW_FLOOR]}  # fmt: skip
    box = apply_verdict(box, verdict_with(first), NOW)
    assert box.status == IDLE and box.host_specs == first
    assert not download_due(box, NOW) and download_due(box, NOW + cfg.FULL_CHECK_INTERVAL_S)
    benched = apply_verdict(box, verdict_with({**first, 'down_below_rounds': 3}, passed=False), NOW + 1)
    assert benched.status == BENCHED and benched.last_failed == [ck.HOST_SPEC]
    assert (
        benched.host_specs['down_mbps'] == 50.0 and benched.host_specs['down_below_rounds'] == 0
    )  # history stays, the count restarts
    # a strike (nothing judged) still moves the record on: the EMA is measurement, not judgement
    not_run = CheckVerdict.from_checks(
        [CheckResult(ck.HOST_SPEC, True, {'host': {**first, 'down_mbps': 80.0}}), CheckResult('gpu_proof', False, {}, not_run=True)],
        [UUID_A], now=NOW,
    )  # fmt: skip
    struck = apply_verdict(
        BoxState(HK_A, status=IDLE, pinned_uuids=[UUID_A], cards={UUID_A: CardState()}), not_run, NOW
    )
    assert struck.not_run_count == 1 and struck.host_specs['down_mbps'] == 80.0
    # a verdict without the check (an older controller's) leaves what the box had
    bare = CheckVerdict.from_checks([CheckResult('gpu_spec', True)], [UUID_A], now=NOW)
    assert apply_verdict(box, bare, NOW).host_specs == first
    assert BoxState.from_dict(box.as_dict()).host_specs == first  # round-trips through the store


def test_the_full_check_feeds_the_record_through_and_the_recorded_box_is_admitted(proof, allowlist):
    verdict = run_full_check(passing_runner(), allowlist, proof, config=CONFIG, host_history={'down_mbps': 100.0, 'down_below_rounds': 0})  # fmt: skip
    assert verdict.admitted
    host = verdict.host
    assert host['ram_gb'] == 135.1 and host['cpu_threads'] == 32 and host['disk_total_gb'] == 1967.8
    assert (
        host['down_mbps'] == 138.2 and host['down_sample_mbps'] == 227.3 and host['shortfalls'] == []
    )  # 0.3 x 227 + 0.7 x 100
    assert verdict.as_dict()['host'] == host
    # three rounds under the floor on the box's record, and this one still under: the normal failed-check path
    slow = passing_runner(download='1250000 29754290 200')  # 10 Mbps
    verdict = run_full_check(slow, allowlist, proof, config=CONFIG, host_history={'down_mbps': 20.0, 'down_below_rounds': 2})  # fmt: skip
    assert verdict.failed == [ck.HOST_SPEC] and verdict.skipped == ['gpu_proof']
    box = apply_verdict(BoxState(HK_A), verdict, NOW)
    assert box.status == BENCHED and box.last_failed_why[ck.HOST_SPEC].startswith('download measured 17 Mbps')


# ---------------------------------------------------------------- published ------------------------------------------


def test_guaranteed_is_the_hard_floors_times_the_size_and_observed_min_the_least_free_box():
    assert guaranteed_host(2, hard=False) == {'ram_gb': None, 'cpu_threads': None, 'down_mbps': 100.0}
    assert guaranteed_host(4, hard=True) == {'ram_gb': 64.0, 'cpu_threads': 16, 'down_mbps': 100.0}
    hosts = [
        {'ram_gb': 135.0, 'cpu_threads': 32, 'down_mbps': 227.0},
        {'ram_gb': 64.0, 'cpu_threads': 48, 'down_mbps': None},
        {'ram_gb': None, 'cpu_threads': 'x', 'down_mbps': 150.0},
    ]
    assert observed_min_host(hosts) == {'ram_gb': 64.0, 'cpu_threads': 32.0, 'down_mbps': 150.0}
    assert observed_min_host([]) == {'ram_gb': None, 'cpu_threads': None, 'down_mbps': None}


def test_the_document_carries_the_host_per_box_and_the_two_objects_per_offer_row(tmp_path):
    boxes, instances = fleet()
    a = boxes[HK_A]
    a.rent_ports = [31000, 31099]
    a.cards = {UUID_A: CardState(IDLE, '', NOW), UUID_B: CardState(IDLE, '', NOW)}
    a.host_specs = {
        'ram_gb': 125.4, 'cpu_threads': 32, 'disk_total_gb': 1967.8, 'down_mbps': 227.3, 'down_sample_mbps': 230.0,
        'down_at': NOW - 100, 'down_below_rounds': 0, 'up_mbps': None, 'shortfalls': [w.CPU_BELOW_FLOOR, 'free text'],
    }  # fmt: skip
    doc = build_fleet(tmp_path, boxes, {}, {}, True, NOW)
    row_a = next(x for x in doc['boxes'] if x['hotkey'] == HK_A)
    assert row_a['host'] == {
        'ram_gb': 125.4, 'cpu_threads': 32.0, 'disk_total_gb': 1967.8, 'down_mbps': 227.3, 'up_mbps': None,
        'shortfalls': [w.CPU_BELOW_FLOOR],  # our codes only; the sample and the stamp stay on the box
    }  # fmt: skip
    assert 'down_sample_mbps' not in row_a['host'] and 'free text' not in str(doc)
    row_b = next(x for x in doc['boxes'] if x['hotkey'] == HK_B)
    assert row_b['host'] == {'ram_gb': None, 'cpu_threads': None, 'disk_total_gb': None, 'down_mbps': None, 'up_mbps': None, 'shortfalls': []}  # fmt: skip
    offer = doc['offers']['RTX5090']['2']
    assert offer == {
        'boxes': 1,
        'guaranteed': {'ram_gb': None, 'cpu_threads': None, 'down_mbps': 100.0},
        'observed_min': {'ram_gb': 125.4, 'cpu_threads': 32.0, 'down_mbps': 227.3},
    }
    with patch.object(cfg, 'HOST_SPEC_HARD', True):
        hard = build_fleet(tmp_path, boxes, {}, {}, True, NOW)['offers']['RTX5090']['2']
    assert hard['guaranteed'] == {'ram_gb': 32.0, 'cpu_threads': 8, 'down_mbps': 100.0}
    assert HK_A not in str(doc['offers'])  # a row names no box
