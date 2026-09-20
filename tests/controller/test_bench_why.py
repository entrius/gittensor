# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``last_failed_why``: why a box is benched, in words, on a page anyone may read.

The page used to show "failed card_free" — the name of the check and nothing a miner could act on. The reason existed
all along, in ``controller.jsonl``, and never left the controller. These tests cover the three things that had to be
true before it could: that every check classifies its own failure, that the classification survives into the state
file and out into ``public/fleet.json``, and — the one that shapes the whole design — that no substring of anything a
box reported can ride out with it. A ``/proc/<pid>/comm`` is 15 bytes of whatever the miner chooses.
"""

import json
from typing import Any, cast

import pytest

from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import why as w
from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from gittensor.controller.checks.state import (
    BENCHED,
    DEREGISTERED,
    UNREACHABLE,
    BoxState,
    apply_deregistered,
    apply_heartbeat_failure,
    apply_unreachable,
    apply_verdict,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.publish import build_fleet
from tests.controller.conftest import DRIVER, NVML_MD5, fixture

HK = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
OURS = 'c' * 64
NOW = 1_789_000_000.0


def phrase_of(result: CheckResult) -> str:
    return w.phrase(result.name, result.evidence)


def published(box: BoxState, tmp_path) -> dict:
    """One box through the real publisher, as das serves it."""
    doc = build_fleet(tmp_path, {box.box_id: box}, {}, {}, False, NOW)
    return doc['boxes'][0]


# ---------------------------------------------------------------- the invariant ---------------------------------------


def test_no_part_of_what_the_box_reported_reaches_the_published_reason(tmp_path):
    """The rule the design exists for, against the real mainnet case (9/20, UID 135): a desktop session holding the
    card. Every comm in the fixture is the miner's to choose; not one byte of any of them may be published."""
    holders = fixture('device_holders_desktop.txt')
    result = ck.check_card_free(holders, ours={OURS})
    assert not result.passed

    comms = [line.split(' ', 2)[2] for line in holders.splitlines() if line.startswith('== ')]
    assert comms == ['Xorg', 'gnome-shell', 'xdg-desktop-por', 'snapd-desktop-i']  # the fixture, as we read it

    # The log keeps every one of them. That is the point of the log.
    for comm in comms:
        assert comm in result.evidence['reason']

    # The published document keeps none of them — nor a PID, nor a cgroup path, nor a device node.
    verdict = CheckVerdict.from_checks([result], [], now=NOW)
    box = apply_verdict(BoxState(HK), verdict, NOW)
    text = json.dumps(published(box, tmp_path))
    for comm in comms:
        assert comm not in text
    for leaked in ('1196', '1642', '2154', '2616', 'user.slice', 'display-manager', '/dev/nvidia', '/proc/'):
        assert leaked not in text, leaked


def test_render_takes_a_constant_and_integers_and_nothing_else():
    """``why.render`` is the enforcement point, not a convention. A check that puts text where a count belongs — or a
    code that is not ours — publishes nothing rather than publishing it."""
    assert w.render({'code': w.DESKTOP_SESSION, 'n': 4}) == (
        'a desktop session is using this GPU (4 processes outside our containers)'
    )
    # A string where a number goes is dropped, so the template has no value for its slot and renders nothing.
    assert w.render({'code': w.DESKTOP_SESSION, 'n': '<script>alert(1)</script>'}) == ''
    # A code the box somehow chose is a dict miss, never echoed back.
    assert w.render({'code': '<img src=x onerror=alert(1)>'}) == ''
    assert w.render({'code': w.DESKTOP_SESSION, 'n': 4, 'comm': 'gnome-shell'}).find('gnome-shell') == -1
    assert w.render(None) == '' and w.render({}) == '' and w.render(cast(Any, 'not a mapping')) == ''
    # Floats and bools are not counts we would print; the first is floored, the second dropped.
    assert w.render({'code': w.DESKTOP_SESSION, 'n': 4.7}).startswith('a desktop session')
    assert w.render({'code': w.DESKTOP_SESSION, 'n': True}) == ''


def test_a_phrase_that_is_not_ours_is_not_published(tmp_path):
    """``publish._PHRASE`` is the second lock: a state file edited by hand, or a future check that skips ``why``,
    still cannot put free text on the page."""
    box = BoxState(HK, status=BENCHED, bench_until=NOW + 60, last_failed=['card_free', 'gpu_proof'])
    box.last_failed_why = {'card_free': '<b>owned</b> by me', 'gpu_proof': w.BY_NAME['gpu_proof']}
    row = published(box, tmp_path)
    assert row['last_failed'] == ['card_free', 'gpu_proof']
    assert row['last_failed_why'] == {'gpu_proof': w.BY_NAME['gpu_proof']}


def test_every_check_name_has_something_to_say():
    """A new check must not be able to reach the page with no words for the miner. Every name a check can be benched
    under is in ``why.BY_NAME``, so the worst case is a general phrase, never a bare check name."""
    names = {
        ck.GPU_SPEC, ck.GPU_UUID_PIN, ck.FLEET_UUID_UNIQUE, ck.NVML_DIGEST, ck.POWER_LIMIT,
        ck.AGENT_IMAGE, ck.DISK_FREE, ck.NETWORK, ck.CARD_FREE, ck.GPU_PROOF,
    }  # fmt: skip
    assert names <= set(w.BY_NAME)
    assert {UNREACHABLE, DEREGISTERED, 'failed_starts', 'external_use'} <= set(w.BY_NAME)
    assert all(w.render({'code': code, 'n': 1, 'low': 1, 'high': 2, 'missing': 1, 'extra': 1, 'floor_gb': 50})
               for code in w.PHRASES)  # fmt: skip


# ---------------------------------------------------------------- card_free ------------------------------------------


def holders(*blocks: tuple[int, str, str]) -> str:
    """``DEVICE_HOLDERS_COMMAND`` output for ``(pid, comm, cgroup)`` holders of one card."""
    fds = ''.join(f'/proc/1/root/proc/{pid}/fd /dev/nvidia0\n' for pid, _, _ in blocks)
    return fds + ''.join(f'== {pid} {comm}\n{cgroup}\n' for pid, comm, cgroup in blocks)


def test_a_desktop_session_is_named_as_one():
    result = ck.check_card_free(fixture('device_holders_desktop.txt'), ours={OURS})
    assert phrase_of(result) == 'a desktop session is using this GPU (4 processes outside our containers)'


def test_one_of_a_thing_reads_as_one():
    """``{s}`` / ``{es}`` pluralise what ``{n}`` counts, so one template covers both — still two constants of ours
    picked by an integer."""
    assert w.render({'code': w.HOST_PROCESS, 'n': 1}).endswith('(1 process outside our containers)')
    assert w.render({'code': w.HOST_PROCESS, 'n': 2}).endswith('(2 processes outside our containers)')
    assert w.render({'code': w.PROOF_CONTAINER, 'n': 1}).endswith('(1 card)')
    assert w.render({'code': w.PROOF_CONTAINER, 'n': 3}).endswith('(3 cards)')


def test_a_container_that_is_not_ours_is_named_as_one():
    other = 'd' * 64
    result = ck.check_card_free(holders((900, 'python3', f'0::/system.slice/docker-{other}.scope')), ours={OURS})
    assert phrase_of(result) == ('a container we did not start is using this GPU (1 process outside our containers)')


def test_an_unrecognised_comm_falls_into_the_generic_bucket():
    """The rule that keeps the vocabulary closed: a category is never invented from the comm. Whatever a miner names
    their process, it is 'another process on this host' until we add a prefix of our own."""
    odd = holders((900, 'zzz-my-own-proc', '0::/'), (901, '<script>alert', '0::/'))
    result = ck.check_card_free(odd, ours={OURS})
    assert phrase_of(result) == ('another process on this host is using this GPU (2 processes outside our containers)')
    assert result.evidence[w.PUBLIC] == {'code': w.HOST_PROCESS, 'n': 2}


def test_a_known_gpu_workload_is_named_as_one():
    result = ck.check_card_free(holders((900, 'ollama', '0::/system.slice/ollama.service')), ours={OURS})
    assert phrase_of(result).startswith('another GPU workload is running on this box')


def test_the_bucket_with_the_most_holders_leads_and_the_count_is_all_of_them():
    mixed = holders(
        (900, 'gnome-shell', '0::/user.slice/user-1000.slice/session-2.scope'),
        (901, 'Xorg', '0::/system.slice/display-manager.service'),
        (902, 'zzz-unknown', '0::/'),
    )
    result = ck.check_card_free(mixed, ours={OURS})
    assert phrase_of(result) == 'a desktop session is using this GPU (3 processes outside our containers)'


def test_a_scan_that_could_not_run_says_so():
    result = ck.check_card_free('', ours=(), scrape_error='exit 3: no host procfs at /proc/1/root/proc')
    assert result.not_run
    assert phrase_of(result) == 'we could not scan this box for processes holding the GPU'
    assert '/proc/1/root' not in phrase_of(result)


# ---------------------------------------------------------------- the other checks ------------------------------------


def test_gpu_proof_says_which_half_of_it_broke():
    """The 9/18 case: a miner's proof container would not start and telling them so took a log dive."""
    nvidia = [{'uuid': 'GPU-a', 'passed': False, 'not_run': True,
               'reason': 'container never started: exit 125: Error response from daemon: ... nvidia-container-cli: '
                         'initialization error'}]  # fmt: skip
    probe = ck.ProbeResult(provider='p', cards=nvidia)
    assert phrase_of(ck.proof_result(probe)) == (
        'the NVIDIA container runtime would not start our GPU proof container (1 card)'
    )
    plain = [{'uuid': 'GPU-a', 'passed': False, 'not_run': True,
              'reason': 'container never started: exit 125: Error response from daemon: no space left'}]  # fmt: skip
    assert phrase_of(ck.proof_result(ck.ProbeResult(provider='p', cards=plain))) == (
        'our GPU proof container would not start on this box (1 card)'
    )
    wrong = [{'uuid': 'GPU-a', 'answered_uuid': 'GPU-b', 'passed': False, 'reason': 'answered for another card'}]
    assert phrase_of(ck.proof_result(ck.ProbeResult(provider='p', cards=wrong))).startswith('a card answered')
    bad = [{'uuid': 'GPU-a', 'answered_uuid': 'GPU-a', 'passed': False, 'reason': 'filled 1 GiB of 32 GiB'}]
    assert phrase_of(ck.proof_result(ck.ProbeResult(provider='p', cards=bad))) == (
        'the GPU proof did not check out on 1 card of this box'
    )
    assert phrase_of(ck.proof_result(ck.ProbeResult(provider='p', error='staging failed: no route to host'))) == (
        'we could not set up the GPU proof on this box'
    )


def test_disk_free_names_our_floor_and_never_their_capacity():
    result = ck.check_disk_free(4.1, min_gb=50.0)
    assert phrase_of(result) == 'free disk space is below the 50 GB floor on the disk Docker uses'
    # How much room a box has left is its operator's business: the floor is ours to say, their reading is not.
    assert result.evidence['free_gb'] == 4.1 and '4 GB free' in result.evidence['reason']
    assert '4' not in phrase_of(result)
    assert phrase_of(ck.check_disk_free(None)) == 'we could not read how much disk space is free on this box'


def test_agent_image_separates_our_fault_from_theirs():
    ours = ck.check_agent_image([], [], observed_id='', allowed_ids=())
    assert 'our side' in phrase_of(ours)  # nothing pinned in config: not the miner's problem to fix
    theirs = ck.check_agent_image(['sha256:' + 'f' * 64], ['sha256:' + 'a' * 64])
    assert phrase_of(theirs) == 'this box runs an agent image we did not publish'
    local = ck.check_agent_image([], ['sha256:' + 'a' * 64])
    assert 'local build' in phrase_of(local)
    unread = ck.check_agent_image([], ['sha256:' + 'a' * 64], scrape_error='no such container')
    assert phrase_of(unread) == 'we could not read which agent image this box runs'
    assert 'no such container' in unread.evidence['reason']


def test_nvml_digest_never_names_the_driver_or_the_digest():
    allowlist = NvmlAllowlist({DRIVER: [NVML_MD5]})
    unknown = allowlist.judge('999.99.99', NVML_MD5)
    assert phrase_of(unknown) == "this box's NVIDIA driver version is not on our vetted list yet"
    assert '999.99.99' not in phrase_of(unknown) and '999.99.99' in unknown.evidence['driver']
    mismatch = allowlist.judge(DRIVER, 'f' * 32)
    assert phrase_of(mismatch) == ("this box's NVIDIA management library is not the one published for its driver")
    assert 'f' * 32 not in phrase_of(mismatch)
    assert phrase_of(allowlist.judge('', NVML_MD5)) == 'this box did not report an NVIDIA driver version'
    assert phrase_of(allowlist.judge(DRIVER, '')) == ("we could not find or hash this box's NVIDIA management library")
    disagrees = allowlist.judge(DRIVER, NVML_MD5, kernel_driver='570.00.00')
    assert 'different NVIDIA driver versions' in phrase_of(disagrees)


def test_gpu_spec_and_the_uuid_pin_count_cards_and_name_nothing():
    from gittensor.controller.checks.config import RTX_5090
    from gittensor.controller.checks.scrape import parse_nvidia_smi

    assert phrase_of(ck.check_gpu_spec([], RTX_5090, scrape_error='command not found')) == (
        'nvidia-smi did not answer on this box'
    )
    assert phrase_of(ck.check_gpu_spec([], RTX_5090)) == 'this box reports 0 GPUs, the pool admits 1 to 8'
    gpus = parse_nvidia_smi(fixture('nvidia_smi_4090.csv'))
    wrong_model = ck.check_gpu_spec(gpus, RTX_5090)
    assert phrase_of(wrong_model) == 'the GPU model on this box is not the one the pool admits (1 card)'
    assert '4090' in wrong_model.evidence['reason'] and '4090' not in phrase_of(wrong_model)

    pinned = ['GPU-' + 'a' * 32, 'GPU-' + 'b' * 32]
    changed = ck.check_uuid_pin(parse_nvidia_smi(fixture('nvidia_smi_5090.csv')), pinned)
    assert phrase_of(changed) == ('the GPUs on this box are not the ones pinned when it was admitted (2 gone, 1 new)')
    dupes = [g for g in parse_nvidia_smi(fixture('nvidia_smi_5090.csv'))] * 2
    assert phrase_of(ck.check_uuid_pin(dupes, pinned)) == 'this box reported the same GPU UUID twice'


def test_power_limit_and_fleet_uniqueness():
    from gittensor.controller.checks.scrape import parse_nvidia_smi

    gpus = parse_nvidia_smi(fixture('nvidia_smi_5090.csv'))
    gpus[0].power_limit_w = 100.0
    assert phrase_of(ck.check_power_limit(gpus)) == 'the power limit is set below the pool floor on 1 card'
    gpus[0].power_limit_w = None
    assert phrase_of(ck.check_power_limit(gpus)) == 'this box did not report a power limit on 1 card'
    assert phrase_of(ck.check_power_limit([])) == 'this box reported no GPUs'

    clash = ck.check_fleet_uuid_unique('hk1', ['GPU-x'], {'hk2': ['GPU-x']})
    assert phrase_of(clash) == 'another box in the pool claims 1 of the GPUs this box reports'
    assert 'hk2' in clash.evidence['reason'] and 'hk2' not in phrase_of(clash)  # the other miner is not named


# ---------------------------------------------------------------- state and publication -------------------------------


def test_the_phrase_is_written_wherever_the_failed_name_is(tmp_path):
    result = ck.check_card_free(fixture('device_holders_desktop.txt'), ours={OURS})
    verdict = CheckVerdict.from_checks([result], [], now=NOW)
    box = apply_verdict(BoxState(HK), verdict, NOW)
    assert box.status == BENCHED and box.last_failed == [ck.CARD_FREE]
    assert box.last_failed_why == {ck.CARD_FREE: phrase_of(result)}
    assert published(box, tmp_path)['last_failed_why'] == {ck.CARD_FREE: phrase_of(result)}


def test_a_passing_check_clears_the_phrase_with_the_name():
    """They are one fact: a box that passes must not keep last round's words."""
    failed = CheckVerdict.from_checks([ck.check_disk_free(1.0, min_gb=50.0)], [], now=NOW)
    box = apply_verdict(BoxState(HK), failed, NOW)
    assert box.last_failed_why
    passing = CheckVerdict.from_checks([ck.check_disk_free(500.0, min_gb=50.0)], ['GPU-' + 'a' * 32], now=NOW)
    after = apply_verdict(box, passing, NOW + 10)
    assert after.last_failed == [] and after.last_failed_why == {}


@pytest.mark.parametrize(
    'apply_it, name',
    [
        (lambda b: apply_unreachable(b, NOW, bench_after=1), UNREACHABLE),
        (lambda b: apply_deregistered(b, NOW), DEREGISTERED),
    ],
)
def test_a_bench_with_no_check_behind_it_still_says_why(apply_it, name, tmp_path):
    box = apply_it(BoxState(HK, status='IDLE'))
    assert box.last_failed == [name] and box.last_failed_why == {name: w.BY_NAME[name]}
    assert published(box, tmp_path)['last_failed_why'] == {name: w.BY_NAME[name]}


# ---------------------------------------------------------------- the colon bug ---------------------------------------


def test_a_heartbeat_bench_is_published_at_all(tmp_path):
    """``publish._names`` filtered on ``^[a-z0-9_]{1,64}$`` while ``apply_heartbeat_failure`` stores
    ``heartbeat:<check>``. The colon failed the pattern, so the most serious class of bench there is — a box caught
    cheating under a live lease — published ``last_failed: []`` and the page showed nothing for it at all."""
    box = apply_heartbeat_failure(BoxState(HK, status='IDLE'), ['card_ours_alone'], NOW)
    assert box.status == BENCHED and box.last_failed == ['heartbeat:card_ours_alone']
    row = published(box, tmp_path)
    assert row['last_failed'] == ['heartbeat:card_ours_alone']
    assert row['last_failed_why'] == {
        'heartbeat:card_ours_alone': 'something outside our workload was using this GPU while it was leased'
    }


def test_every_heartbeat_question_has_a_phrase(tmp_path):
    box = apply_heartbeat_failure(BoxState(HK, status='IDLE'), ['same_card', 'our_container'], NOW)
    row = published(box, tmp_path)
    assert row['last_failed'] == ['heartbeat:same_card', 'heartbeat:our_container']
    assert set(row['last_failed_why']) == set(row['last_failed'])


def test_the_widened_pattern_still_refuses_anything_else(tmp_path):
    """One colon between two check names, and nothing more: the pattern was widened, not opened."""
    box = BoxState(HK, status=BENCHED, bench_until=NOW + 60)
    box.last_failed = ['heartbeat:card_ours_alone', 'a:b:c', 'Caps', 'with space', '../etc/passwd', 'x' * 200]
    assert published(box, tmp_path)['last_failed'] == ['heartbeat:card_ours_alone']
