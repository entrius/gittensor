# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``card_free``: exclusivity asked in the round, not only under a lease.

The judge is the heartbeat's own (``checks.foreign_holders``), so a box whose card something else holds is caught on
the pass that already runs every 20 min instead of waiting for a rotation to put work on it. The real mainnet case
(9/19) is the fixture: a desktop session on the card, benched by the heartbeat only after the box had drawn standby
pay. A foreign holder benches on the ladder like any other failed check; a scan that could not run is a strike.
"""

from gittensor.controller.checks import checks as ck
from gittensor.controller.checks.full_check import run_full_check
from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.checks.scrape import DEVICE_HOLDERS_COMMAND, scrape_host
from gittensor.controller.checks.verdict import BENCH, NOT_RUN, CheckResult, CheckVerdict
from tests.controller.conftest import (
    CONFIG,
    NO_DEVICE_HOLDERS,
    fixture,
    passing_runner,
)

OURS = 'c' * 64


def check(verdict: CheckVerdict, name: str) -> CheckResult:
    """``verdict.check(name)`` for a check the test knows ran."""
    result = verdict.check(name)
    assert result is not None, name
    return result


def ours_holding(*devices: str, container: str = OURS) -> str:
    """One of our own instance's containers with the card open — what a box carrying our workload looks like."""
    fds = ''.join(f'/proc/1/root/proc/900/fd {d}\n' for d in devices or ('/dev/nvidiactl', '/dev/nvidia0'))
    return f'{fds}== 900 python3\n0::/system.slice/docker-{container}.scope\n'


# ---------------------------------------------------------------- the judge -------------------------------------------


def test_a_box_nothing_holds_a_card_on_is_free():
    result = ck.check_card_free(NO_DEVICE_HOLDERS, ours=())
    assert result.passed and not result.not_run
    assert result.evidence['holders'] == [] and result.evidence['reason'] == '0 device holder(s), none foreign'
    assert result.name == 'card_free'


def test_only_the_drivers_persistence_daemon_is_free():
    result = ck.check_card_free(fixture('device_holders_persistenced.txt'), ours=())
    assert result.passed and result.evidence['holders'] == [1401]
    assert result.evidence['reason'] == '1 device holder(s), none foreign'


def test_our_own_instances_container_is_not_foreign():
    assert ck.check_card_free(ours_holding(), ours={OURS}).passed
    # the same box with no instance recorded on it: the container is not ours to exempt
    stranger = ck.check_card_free(ours_holding(), ours=())
    assert not stranger.passed and f'pid 900 (python3) in {OURS[:12]} holds' in stranger.evidence['reason']


def test_a_desktop_session_on_the_card_is_a_foreign_holder():
    """The mainnet 9/19 bench line, judged by the round instead of the heartbeat."""
    result = ck.check_card_free(fixture('device_holders_desktop.txt'), ours={OURS})
    assert not result.passed and not result.not_run
    assert result.evidence['holders'] == [1196, 1642, 2154, 2616]
    assert result.evidence['reason'] == (
        'foreign device holder(s): pid 1196 (Xorg) in no container holds /dev/nvidiactl, /dev/nvidia0; '
        'pid 1642 (gnome-shell) in no container holds /dev/nvidiactl, /dev/nvidia0; '
        'pid 2154 (xdg-desktop-por) in no container holds /dev/nvidiactl, /dev/nvidia0; '
        'pid 2616 (snapd-desktop-i) in no container holds /dev/nvidiactl, /dev/nvidia0'
    )


def test_a_holder_whose_cgroup_was_not_read_fails_closed():
    """The fd scan saw it; no block came back for it. Unattributable is foreign: the miner controls that file."""
    result = ck.check_card_free('/proc/1/root/proc/77/fd /dev/nvidia0\n', ours={OURS})
    assert not result.passed and not result.not_run
    assert result.evidence['reason'] == ('foreign device holder(s): pid 77 holds /dev/nvidia0: its cgroup was not read')


def test_a_holder_that_exits_mid_scan_holds_nothing_now():
    """``MISSING``: gone between the fd scan and its cgroup read, so it is not on the card any more."""
    result = ck.check_card_free('/proc/1/root/proc/88/fd /dev/nvidia0\n== 88 sleep\nMISSING\n', ours={OURS})
    assert result.passed and result.evidence['exited_mid_scan'] == [88]
    assert result.evidence['reason'] == '1 device holder(s), none foreign'


def test_a_scan_that_could_not_run_is_no_answer_never_a_bench():
    ran = ck.check_card_free('', ours=(), scrape_error='exit 3: no host procfs at /proc/1/root/proc')
    assert not ran.passed and ran.not_run  # NOT_RUN: a strike, not a bench (9/19)
    assert 'no host procfs' in ran.evidence['reason']


# ---------------------------------------------------------------- in the round ----------------------------------------


def test_the_scrape_carries_the_raw_stdout_and_the_judge_parses_it():
    """Raw stdout in the scrape, parsing in the judge: nothing about a holder is decided while collecting."""
    desktop = fixture('device_holders_desktop.txt')
    scrape = scrape_host(passing_runner(device_holders=desktop), network_targets=CONFIG.network_targets)
    assert scrape.device_holders == desktop and scrape.errors == {}


def test_a_desktop_box_is_benched_by_the_round(proof, allowlist):
    runner = passing_runner(device_holders=fixture('device_holders_desktop.txt'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == BENCH and verdict.failed == [ck.CARD_FREE]
    assert verdict.skipped == [ck.GPU_PROOF]  # nothing is staged on a box that already failed identity
    assert 'gnome-shell' in check(verdict, ck.CARD_FREE).evidence['reason']


def test_an_unscannable_box_is_a_strike_not_a_bench(proof, allowlist):
    no_procfs = CommandResult(3, '', 'no host procfs at /proc/1/root/proc')  # the agent lost --pid host
    runner = passing_runner().on(DEVICE_HOLDERS_COMMAND, no_procfs)
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == NOT_RUN and verdict.failed == [] and verdict.not_run == [ck.CARD_FREE]
    assert 'no host procfs' in check(verdict, ck.CARD_FREE).evidence['reason']
    assert check(verdict, ck.GPU_PROOF).passed  # nothing failed, so the proof still ran: the box is otherwise fine


def test_the_scan_runs_once_in_the_pass_that_already_visits_the_box(proof, allowlist):
    """No new connection, no container, no GPU call: one more read-only command in the scrape."""
    runner = passing_runner()
    run_full_check(runner, allowlist, proof, config=CONFIG)
    scans = [c for c in runner.calls if c == DEVICE_HOLDERS_COMMAND]
    assert len(scans) == 1
    # and it is done before the proof's first container exists, so our own proof can never be a foreign holder
    first_create = next(i for i, c in enumerate(runner.calls) if c.startswith('docker create'))
    assert runner.calls.index(DEVICE_HOLDERS_COMMAND) < first_create
