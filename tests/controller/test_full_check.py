# The MIT License (MIT)
# Copyright © 2025 Entrius

"""run_full_check end to end over a FakeRunner: a recorded 5090 box is admitted; every way a box can be wrong is
benched with the right check named, and the GPU proof is never staged on a box that already failed identity."""

from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import why
from gittensor.controller.checks.full_check import FullCheckConfig, run_full_check
from gittensor.controller.checks.rent_probe import listener_run_command, probe_rent_ports
from gittensor.controller.checks.runner import CommandResult, FakeRunner, regex
from gittensor.controller.checks.scrape import (
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    agent_image_command,
    disk_free_command,
    network_command,
    nvidia_smi_command,
)
from gittensor.controller.checks.verdict import ADMIT, BENCH, NOT_RUN, CheckResult, CheckVerdict
from gittensor.controller.proof.slot import UnconfiguredProof
from tests.controller.conftest import (
    AGENT_IMAGE_ID,
    CONFIG,
    DRIVER,
    FAKE_BINARY,
    GOOD_WALL_MS,
    NETWORK_TARGETS,
    PROOF_IMAGE,
    UUID_5090,
    UUID_5090_B,
    container_for,
    failing,
    fixture,
    job_responder,
    passing_runner,
)

ALL_CHECKS = [
    ck.GPU_SPEC,
    ck.GPU_UUID_PIN,
    ck.NVML_DIGEST,
    ck.POWER_LIMIT,
    ck.AGENT_IMAGE,
    ck.DISK_FREE,
    ck.CARD_FREE,
    ck.NETWORK,
    ck.GPU_PROOF,
]


def proof_calls(runner):
    return [c for c in runner.calls if c.startswith(('docker create', 'docker cp', 'docker start', 'docker rm'))]


def check(verdict: CheckVerdict, name: str) -> CheckResult:
    """``verdict.check(name)`` for a check the test knows ran."""
    result = verdict.check(name)
    assert result is not None, name
    return result


def test_real_5090_fixture_is_admitted(proof, allowlist):
    runner = passing_runner()
    verdict = run_full_check(runner, allowlist, proof, pinned_uuids=None, config=CONFIG, now=1_000.0)
    assert verdict.verdict == ADMIT and verdict.admitted and verdict.failed == [] and verdict.skipped == []
    assert [c.name for c in verdict.checks] == ALL_CHECKS and all(c.passed for c in verdict.checks)
    assert (
        verdict.gpu_uuids == [UUID_5090] and verdict.card_name == 'NVIDIA GeForce RTX 5090' and verdict.driver == DRIVER
    )
    assert verdict.checked_at == 1_000.0
    gpu_proof = check(verdict, ck.GPU_PROOF)
    (card,) = gpu_proof.evidence['cards']
    assert gpu_proof.evidence['provider'] == 'fake-1' and card['uuid'] == UUID_5090 and card['reason'] == 'ok'
    assert card['command'] == f'docker start -a {container_for(UUID_5090)}'
    # the two phases on the box: create pinned to the card + the binary copied in, then start, then cleanup
    calls = proof_calls(runner)
    assert calls[0].startswith(f'docker create --gpus="device={UUID_5090}"') and PROOF_IMAGE in calls[0]
    assert calls[1].startswith('docker cp -') and runner.stdins[calls[1]] == FAKE_BINARY
    assert calls[2] == card['command']
    assert calls[3] == f'docker rm -f {container_for(UUID_5090)}'
    assert len(proof.staged) == 1
    d = verdict.as_dict()
    assert d['verdict'] == 'ADMIT' and d['checks'][0] == {
        'name': 'gpu_spec',
        'pass': True,
        'evidence': d['checks'][0]['evidence'],
    }


def test_admitted_box_re_checked_against_its_pin(proof, allowlist):
    verdict = run_full_check(passing_runner(), allowlist, proof, pinned_uuids=[UUID_5090], config=CONFIG)
    assert verdict.admitted and check(verdict, ck.GPU_UUID_PIN).evidence['pinned'] == [UUID_5090]


def test_two_card_box_is_staged_once_and_fired_on_both_cards(proof, allowlist):
    runner = passing_runner(nvidia_smi=fixture('nvidia_smi_2x5090.csv'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.admitted and verdict.gpu_uuids == [UUID_5090, UUID_5090_B]
    cards = check(verdict, ck.GPU_PROOF).evidence['cards']
    assert [c['uuid'] for c in cards] == [UUID_5090, UUID_5090_B] and all(c['passed'] for c in cards)
    calls = proof_calls(runner)
    creates = [c for c in calls if c.startswith('docker create')]
    starts = [c for c in calls if c.startswith('docker start')]
    assert len(creates) == 2 and len(starts) == 2 and len(proof.staged) == 1
    assert calls.index(starts[0]) > calls.index(creates[1])  # phase 2 begins only after phase 1 is done on every card
    assert calls[-1] == f'docker rm -f {container_for(UUID_5090)} {container_for(UUID_5090_B)}'


def test_wrong_gpu_model_fails_gpu_spec_and_stages_nothing(proof, allowlist):
    runner = passing_runner(nvidia_smi=fixture('nvidia_smi_4080.csv'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_SPEC] and verdict.skipped == [ck.GPU_PROOF]
    reason = check(verdict, ck.GPU_SPEC).evidence['reason']
    assert "'NVIDIA GeForce RTX 4080'" in reason and 'not in the GPU catalog' in reason
    # a listed type (a 4090, 10/8) is refused the same way, naming the type
    verdict = run_full_check(passing_runner(nvidia_smi=fixture('nvidia_smi_4090.csv')), allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.GPU_SPEC] and '(RTX4090) is listed but not qualified' in check(verdict, ck.GPU_SPEC).evidence['reason']  # fmt: skip
    assert proof.staged == [] and proof_calls(runner) == []


def test_no_provider_in_the_slot_admits_nobody(allowlist):
    runner = passing_runner()
    verdict = run_full_check(runner, allowlist, UnconfiguredProof(), config=CONFIG)
    assert verdict.verdict == NOT_RUN and not verdict.admitted and verdict.not_run == [ck.GPU_PROOF]
    ev = check(verdict, ck.GPU_PROOF).evidence
    assert 'no GPU proof provider configured' in ev['reason'] and ev['provider'] == 'unconfigured'
    assert proof_calls(runner) == []
    # and the default argument is that same fail-closed provider
    assert run_full_check(passing_runner(), allowlist, config=CONFIG).not_run == [ck.GPU_PROOF]


def test_extra_uuid_fails_the_pin(proof, allowlist):
    runner = passing_runner(nvidia_smi=fixture('nvidia_smi_2x5090.csv'))
    verdict = run_full_check(runner, allowlist, proof, pinned_uuids=[UUID_5090], config=CONFIG)
    assert verdict.failed == [ck.GPU_UUID_PIN]
    assert check(verdict, ck.GPU_UUID_PIN).evidence['extra'] == [UUID_5090_B]


def test_missing_and_swapped_uuid_fail_the_pin(proof, allowlist):
    verdict = run_full_check(passing_runner(), allowlist, proof, pinned_uuids=[UUID_5090, UUID_5090_B], config=CONFIG)
    assert verdict.failed == [ck.GPU_UUID_PIN] and check(verdict, ck.GPU_UUID_PIN).evidence['missing'] == [UUID_5090_B]
    verdict = run_full_check(passing_runner(), allowlist, proof, pinned_uuids=['GPU-old-card'], config=CONFIG)
    assert verdict.failed == [ck.GPU_UUID_PIN]


def test_unknown_driver_fails_closed(proof, allowlist):
    smi = fixture('nvidia_smi_5090.csv').replace(DRIVER, '999.99.99')
    kernel = fixture('proc_driver_version.txt').replace(DRIVER, '999.99.99')
    runner = passing_runner(nvidia_smi=smi, kernel_driver=kernel)
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.NVML_DIGEST]
    assert 'unknown driver' in check(verdict, ck.NVML_DIGEST).evidence['reason']
    assert check(verdict, ck.NVML_DIGEST).evidence['driver'] == '999.99.99'


def test_empty_driver_string_fails_closed(proof, allowlist):
    """The Lium bug from `22`: an empty driver string must not skip the digest check."""
    smi = fixture('nvidia_smi_5090.csv').replace(DRIVER, '')
    runner = passing_runner(nvidia_smi=smi).on(
        KERNEL_DRIVER_COMMAND, failing('cat: /proc/driver/nvidia/version: No such file')
    )
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert (
        verdict.failed == [ck.NVML_DIGEST]
        and check(verdict, ck.NVML_DIGEST).evidence['reason'] == 'empty driver string'
    )


def test_patched_nvml_lib_and_shimmed_nvidia_smi_fail(proof, allowlist):
    runner = passing_runner(nvml_md5='0' * 32 + '  /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1\n')
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.NVML_DIGEST] and check(verdict, ck.NVML_DIGEST).evidence['reason'] == 'digest mismatch'
    runner = passing_runner(kernel_driver=fixture('proc_driver_version.txt').replace(DRIVER, '575.64.03'))
    assert run_full_check(runner, allowlist, proof, config=CONFIG).failed == [ck.NVML_DIGEST]
    runner = passing_runner().on(NVML_MD5_COMMAND, failing(''))
    assert (
        'not found' in check(run_full_check(runner, allowlist, proof, config=CONFIG), ck.NVML_DIGEST).evidence['reason']
    )


def test_low_power_limit_fails(proof, allowlist):
    smi = fixture('nvidia_smi_5090.csv').replace('575.00, 575.00, 600.00', '450.00, 575.00, 600.00')
    verdict = run_full_check(passing_runner(nvidia_smi=smi), allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.POWER_LIMIT]
    ev = check(verdict, ck.POWER_LIMIT).evidence
    assert 'below floor' in ev['reason'] and ev['readings'][0]['ratio'] == round(450 / 575, 4)
    # exactly 90% passes; an unreported limit fails closed
    smi = fixture('nvidia_smi_5090.csv').replace('575.00, 575.00, 600.00', '517.50, 575.00, 600.00')
    assert run_full_check(passing_runner(nvidia_smi=smi), allowlist, proof, config=CONFIG).admitted
    smi = fixture('nvidia_smi_5090.csv').replace('575.00, 575.00, 600.00', '[N/A], [N/A], [N/A]')
    verdict = run_full_check(passing_runner(nvidia_smi=smi), allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.POWER_LIMIT] and check(verdict, ck.POWER_LIMIT).evidence['incomplete'] == [UUID_5090]


def test_proof_sealed_for_another_box_fails(proof, allowlist):
    runner = passing_runner(job=job_responder(challenge='0' * 32))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_PROOF]
    assert 'challenge did not echo' in check(verdict, ck.GPU_PROOF).evidence['reason']
    assert proof_calls(runner)[-1].startswith('docker rm -f')  # cleanup runs either way


def test_slow_proof_fails(proof, allowlist):
    runner = passing_runner(job=job_responder(wall_ms=1.6 * GOOD_WALL_MS + 1))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'too slow' in check(verdict, ck.GPU_PROOF).evidence['reason']
    runner = passing_runner(job=job_responder(wall_ms=1.6 * GOOD_WALL_MS - 1))
    assert run_full_check(runner, allowlist, proof, config=CONFIG).admitted


def test_our_own_clock_bounds_the_proof(proof, allowlist):
    """A relay to a card elsewhere pays the round trip: `docker start -a` took 20 s by our clock, whatever the sealed
    result says (the fake provider's outer budget is 6 s); 5 s is an honest box's container start."""
    from gittensor.controller.checks.scrape import scrape_host

    ticks = iter([0.0, 20.0])
    scrape = scrape_host(passing_runner(), network_targets=NETWORK_TARGETS)
    result = ck.check_gpu_proof(passing_runner(), scrape.gpus, proof, PROOF_IMAGE, clock=lambda: next(ticks))
    assert not result.passed and 'too slow: 20000 ms round trip > 6000 ms' in result.evidence['reason']
    assert result.evidence['cards'][0]['elapsed_ms'] == 20000.0
    ticks = iter([0.0, 5.0])
    result = ck.check_gpu_proof(passing_runner(), scrape.gpus, proof, PROOF_IMAGE, clock=lambda: next(ticks))
    assert result.passed and result.evidence['cards'][0]['elapsed_ms'] == 5000.0


def test_proof_answered_by_another_card_or_underfilled_fails(proof, allowlist):
    runner = passing_runner(job=job_responder(uuid='GPU-relay-target'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and f'not {UUID_5090}' in check(verdict, ck.GPU_PROOF).evidence['reason']
    runner = passing_runner(job=job_responder(filled_bytes=8_000_000_000))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'under-filled' in check(verdict, ck.GPU_PROOF).evidence['reason']


def test_a_proof_that_ran_and_failed_is_a_failure(proof, allowlist):
    runner = passing_runner().on(
        regex(r'^docker start '), failing('{"error":"nothing staged at /opt/gt-proof/bin/gt_proof"}')
    )
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_PROOF]
    assert 'job error' in check(verdict, ck.GPU_PROOF).evidence['reason']
    # the transport dying mid-proof stays a failure: the challenge was out
    runner = passing_runner().on(regex(r'^docker start '), TimeoutError('ssh: timed out'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'TimeoutError' in check(verdict, ck.GPU_PROOF).evidence['reason']


OCI_ERROR = (
    'Error response from daemon: failed to create task for container: failed to create shim task: OCI runtime create '
    'failed: runc create failed: unable to start container process: error during container init: error running '
    "prestart hook #0: exit status 1, stdout: , stderr: Auto-detected mode as 'legacy'\n"
    'nvidia-container-cli: initialization error: nvml error: driver/library version mismatch: unknown'
)


def test_a_proof_that_could_not_run_is_not_a_failure(proof, allowlist):
    """Mainnet 9/19: NVIDIA's prestart hook failed on a miner's box, the proof never ran, and it was benched 64 h as a
    failed GPU proof, with the hook's reason cut off at 300 characters."""
    runner = passing_runner().on(regex(r'^docker start '), failing(OCI_ERROR))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == NOT_RUN and not verdict.admitted
    assert verdict.failed == [] and verdict.not_run == [ck.GPU_PROOF]
    result = check(verdict, ck.GPU_PROOF)
    assert result.not_run and not result.passed and result.as_dict()['not_run'] is True
    assert 'container never started' in result.evidence['reason']
    assert 'driver/library version mismatch' in result.evidence['reason']  # the end of the error is what is kept
    # a failed stage is the same: nothing ran
    runner = passing_runner().on(regex(r'^docker create '), failing('docker: no such image'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    ev = check(verdict, ck.GPU_PROOF).evidence
    assert verdict.not_run == [ck.GPU_PROOF] and 'docker create failed' in ev['reason'] and ev['cards'] == []
    runner = passing_runner().on(regex(r'^docker create '), ConnectionError('ssh: connection reset'))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == NOT_RUN
    assert 'staging failed: ConnectionError' in check(verdict, ck.GPU_PROOF).evidence['reason']


def test_a_named_failure_wins_over_a_check_that_could_not_run():
    checks = [CheckResult('gpu_spec', False, {'reason': 'a 4090'}), CheckResult(ck.GPU_PROOF, False, {}, not_run=True)]
    verdict = CheckVerdict.from_checks(checks, [])
    assert verdict.verdict == BENCH and verdict.failed == ['gpu_spec'] and verdict.not_run == [ck.GPU_PROOF]


def test_agent_image_digest_disk_and_network(proof, allowlist):
    runner = passing_runner(agent_image='entrius/gt-agent@sha256:' + 'e' * 64 + '\n')
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert (
        verdict.failed == [ck.AGENT_IMAGE]
        and 'not one we published' in check(verdict, ck.AGENT_IMAGE).evidence['reason']
    )
    runner = passing_runner().on(agent_image_command(), failing('Error: No such object: gt-agent'))
    assert run_full_check(runner, allowlist, proof, config=CONFIG).failed == [ck.AGENT_IMAGE]
    # no digest pinned in config -> fail closed
    unpinned = FullCheckConfig(agent_image_digests=(), network_targets=NETWORK_TARGETS, proof_image=PROOF_IMAGE)
    assert run_full_check(passing_runner(), allowlist, proof, config=unpinned).failed == [ck.AGENT_IMAGE]
    runner = passing_runner(
        df='Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000000 900000 50000000 90% /\n'
    )
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.DISK_FREE] and '< 100 GB' in check(verdict, ck.DISK_FREE).evidence['reason']
    runner = passing_runner().on(disk_free_command(), failing('df: /var/lib/docker: No such file or directory'))
    assert run_full_check(runner, allowlist, proof, config=CONFIG).failed == [ck.DISK_FREE]
    # the network is evidence, never a failure (mainnet 9/19: one Hugging Face miss emptied the fleet for 4 h)
    runner = passing_runner().on(network_command(NETWORK_TARGETS[1]), failing('curl: (6) Could not resolve host', 6))
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.admitted and check(verdict, ck.NETWORK).evidence['unreachable'] == [NETWORK_TARGETS[1]]


def test_several_failures_are_all_named(proof, allowlist):
    smi = fixture('nvidia_smi_4090.csv').replace(DRIVER, '1.2.3')
    runner = passing_runner(nvidia_smi=smi, kernel_driver='NVRM version: Kernel Module 1.2.3\n')
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.failed == [ck.GPU_SPEC, ck.NVML_DIGEST] and verdict.skipped == [ck.GPU_PROOF]


def test_no_gpu_at_all(proof, allowlist):
    runner = passing_runner().on(
        nvidia_smi_command(),
        failing("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.", 9),
    )
    verdict = run_full_check(runner, allowlist, proof, config=CONFIG)
    assert verdict.verdict == BENCH and ck.GPU_SPEC in verdict.failed and ck.POWER_LIMIT in verdict.failed
    assert verdict.gpu_uuids == [] and proof.staged == []
    assert 'nvidia-smi' in check(verdict, ck.GPU_SPEC).evidence['reason']


# ---------------------------------------------------------------- the rent-range probe (29 §5) ------------------------

LISTENER = regex(r'^docker rm -f gt-rent-probe-\d+ >/dev/null 2>&1; docker run --rm -d --name gt-rent-probe-\d+ ')
RUNNING = "docker inspect --format '{{.State.Running}}' gt-rent-probe-31099"
REMOVE = 'docker rm -f gt-rent-probe-31099'


class Clock:
    """Time that only moves when the probe sleeps."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def listener_runner(running='true\n'):
    return FakeRunner({LISTENER: 'c' * 64 + '\n', RUNNING: running, regex(r'^docker rm -f gt-rent-probe-\d+$'): ''})


def probe(runner, dial, host='203.0.113.7', ports=(31000, 31099), image=AGENT_IMAGE_ID, clock=None, **kw):
    clock = clock or Clock()
    return probe_rent_ports(runner, host, list(ports), image, dial=dial, clock=clock.now, sleep=clock.sleep, **kw)


def test_the_rent_probe_starts_a_listener_on_the_top_port_dials_it_where_the_box_is_public_and_removes_it():
    runner, dialled = listener_runner(), []
    result = probe(runner, lambda host, port: dialled.append((host, port)) or True)
    assert result.ok and (result.port, result.public_port, result.code, result.reason) == (31099, 31099, '', '')
    assert dialled == [('203.0.113.7', 31099)]  # the top of the range: pods take the bottom first
    run, rm = runner.calls
    assert run == listener_run_command(AGENT_IMAGE_ID, 31099) and rm == REMOVE
    assert '-p 31099:31099' in run and '-e GT_AGENT_SSH_PORT=31099' in run and ' ' + 'b' * 64 + ' ' in run
    assert 'sha256:' not in run and '--entrypoint timeout' in run and run.endswith('/entrypoint.sh')
    assert result.as_dict() == {
        'host': '203.0.113.7', 'port': 31099, 'public_port': 31099, 'ok': True, 'reason': '', 'code': '',
    }  # fmt: skip

    # a dev box behind a remapping host (a Lium pod): published on the range's port, dialled on the mapped one
    dialled.clear()
    result = probe(listener_runner(), lambda h, p: dialled.append((h, p)) or True, host='10.0.0.1',
                   ports=(31000, 31003), public_port_of={31003: 45003}.__getitem__)  # fmt: skip
    assert result.ok and (result.port, result.public_port) == (31003, 45003) and dialled == [('10.0.0.1', 45003)]


def test_a_range_the_controller_cannot_reach_is_told_apart_from_a_listener_that_would_not_start():
    # closed: the listener runs, nothing answers the dial until the deadline, the listener is still removed
    runner, clock = listener_runner(), Clock()
    result = probe(runner, lambda h, p: False, clock=clock, timeout_s=3.0)
    assert not result.ok and result.code == why.RENT_PORT_UNREACHABLE
    assert result.reason == '203.0.113.7:31099 unreachable from the controller (firewall?)'
    assert clock.t >= 3.0 and runner.calls[1] == RUNNING and runner.calls[-1] == REMOVE

    # the listener exited (the image refused to start): not the firewall's doing
    result = probe(listener_runner(running='false\n'), lambda h, p: False, timeout_s=0.0)
    assert not result.ok and result.code == why.RENT_LISTENER_FAILED and 'exited' in result.reason

    # docker refused the run: the error is kept for the operator, the name is still cleaned up
    runner = FakeRunner({LISTENER: CommandResult(125, '', 'port is already allocated'), REMOVE: ''})
    result = probe(runner, lambda h, p: True)
    assert not result.ok and result.code == why.RENT_LISTENER_FAILED and 'already allocated' in result.reason
    assert runner.calls[-1] == REMOVE

    # no agent image id to run from, or a transport that died: the same answer, no dial
    assert probe(FakeRunner(), lambda h, p: True, image='').code == why.RENT_LISTENER_FAILED
    result = probe(FakeRunner({LISTENER: OSError('connection reset')}), lambda h, p: True)
    assert not result.ok and result.code == why.RENT_LISTENER_FAILED and 'connection reset' in result.reason

    # the phrases are ours alone and carry no port
    for code in (why.RENT_PORT_UNREACHABLE, why.RENT_LISTENER_FAILED):
        text = why.render({'code': code})
        assert text and 'idle only' in text and '31099' not in text and '?' not in text


def test_a_range_the_probe_could_not_reach_is_no_range_in_the_verdict_and_never_a_failure():
    checks = [CheckResult(ck.GPU_SPEC, True)]
    closed = probe(listener_runner(), lambda h, p: False, timeout_s=0.0).as_dict()
    verdict = CheckVerdict.from_checks(checks, [UUID_5090], rent_ports=[31000, 31099], rent_probe=closed)
    assert verdict.admitted and verdict.failed == [] and verdict.rent_ports == [] and verdict.rent_probe == closed
    assert verdict.as_dict()['rent_probe'] == closed
    opened = probe(listener_runner(), lambda h, p: True).as_dict()
    assert CheckVerdict.from_checks(checks, [UUID_5090], rent_ports=[31000, 31099], rent_probe=opened).rent_ports == [31000, 31099]  # fmt: skip
    unprobed = CheckVerdict.from_checks(checks, [UUID_5090], rent_ports=[31000, 31099])  # run_full_check: no address
    assert unprobed.rent_ports == [31000, 31099] and unprobed.rent_probe is None
