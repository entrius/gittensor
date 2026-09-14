# The MIT License (MIT)
# Copyright © 2025 Entrius

"""run_full_check end to end over a FakeRunner: a recorded 5090 box is admitted; every way a box can be wrong is
benched with the right check named and no bank seed wasted on a box that already failed identity."""

from gittensor.controller.checks import checks as ck
from gittensor.controller.checks.full_check import FullCheckConfig, run_full_check
from gittensor.controller.checks.runner import regex
from gittensor.controller.checks.scrape import (
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    agent_image_command,
    disk_free_command,
    network_command,
    nvidia_smi_command,
)
from gittensor.controller.checks.verdict import ADMIT, BENCH
from tests.controller.conftest import (
    CONFIG,
    DRIVER,
    NETWORK_TARGETS,
    UUID_5090,
    UUID_5090_B,
    failing,
    fixture,
    job_responder,
    make_bank,
    passing_runner,
)

ALL_CHECKS = [
    ck.GPU_SPEC,
    ck.GPU_UUID_PIN,
    ck.NVML_DIGEST,
    ck.POWER_LIMIT,
    ck.AGENT_IMAGE,
    ck.DISK_FREE,
    ck.NETWORK,
    ck.GPU_PROOF,
]


def test_real_5090_fixture_is_admitted(bank, allowlist):
    runner = passing_runner(bank)
    verdict = run_full_check(runner, allowlist, bank, pinned_uuids=None, config=CONFIG, now=1_000.0)
    assert verdict.verdict == ADMIT and verdict.admitted and verdict.failed == [] and verdict.skipped == []
    assert [c.name for c in verdict.checks] == ALL_CHECKS and all(c.passed for c in verdict.checks)
    assert (
        verdict.gpu_uuids == [UUID_5090] and verdict.card_name == 'NVIDIA GeForce RTX 5090' and verdict.driver == DRIVER
    )
    assert verdict.checked_at == 1_000.0
    proof = verdict.check(ck.GPU_PROOF)
    assert proof.evidence['cards'][0]['seed'] == 1000 and proof.evidence['cards'][0]['reason'] == 'ok'
    assert bank.remaining == 4  # one seed spent, one card
    docker_runs = [c for c in runner.calls if c.startswith('docker run')]
    assert (
        len(docker_runs) == 1 and f'--gpus="device={UUID_5090}"' in docker_runs[0] and '--seed 1000' in docker_runs[0]
    )
    assert 'ghcr.io/entrius/gt-challenge:dev' in docker_runs[0]
    d = verdict.as_dict()
    assert d['verdict'] == 'ADMIT' and d['checks'][0] == {
        'name': 'gpu_spec',
        'pass': True,
        'evidence': d['checks'][0]['evidence'],
    }


def test_admitted_box_re_checked_against_its_pin(bank, allowlist):
    verdict = run_full_check(passing_runner(bank), allowlist, bank, pinned_uuids=[UUID_5090], config=CONFIG)
    assert verdict.admitted and verdict.check(ck.GPU_UUID_PIN).evidence['pinned'] == [UUID_5090]


def test_two_card_box_spends_two_seeds(bank, allowlist):
    runner = passing_runner(bank, nvidia_smi=fixture('nvidia_smi_2x5090.csv'))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.admitted and verdict.gpu_uuids == [UUID_5090, UUID_5090_B] and bank.remaining == 3
    seeds = [c['seed'] for c in verdict.check(ck.GPU_PROOF).evidence['cards']]
    assert seeds == [1000, 1001]


def test_wrong_gpu_model_fails_gpu_spec_and_spends_no_seed(bank, allowlist):
    runner = passing_runner(bank, nvidia_smi=fixture('nvidia_smi_4090.csv'))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_SPEC] and verdict.skipped == [ck.GPU_PROOF]
    reason = verdict.check(ck.GPU_SPEC).evidence['reason']
    assert "'NVIDIA GeForce RTX 4090'" in reason and 'compute_cap' in reason and 'VRAM 24564' in reason
    assert bank.remaining == 5 and not any(c.startswith('docker run') for c in runner.calls)


def test_extra_uuid_fails_the_pin(bank, allowlist):
    runner = passing_runner(bank, nvidia_smi=fixture('nvidia_smi_2x5090.csv'))
    verdict = run_full_check(runner, allowlist, bank, pinned_uuids=[UUID_5090], config=CONFIG)
    assert verdict.failed == [ck.GPU_UUID_PIN]
    assert verdict.check(ck.GPU_UUID_PIN).evidence['extra'] == [UUID_5090_B]


def test_missing_and_swapped_uuid_fail_the_pin(bank, allowlist):
    verdict = run_full_check(
        passing_runner(bank), allowlist, bank, pinned_uuids=[UUID_5090, UUID_5090_B], config=CONFIG
    )
    assert verdict.failed == [ck.GPU_UUID_PIN] and verdict.check(ck.GPU_UUID_PIN).evidence['missing'] == [UUID_5090_B]
    verdict = run_full_check(passing_runner(bank), allowlist, bank, pinned_uuids=['GPU-old-card'], config=CONFIG)
    assert verdict.failed == [ck.GPU_UUID_PIN]


def test_unknown_driver_fails_closed(bank, allowlist):
    smi = fixture('nvidia_smi_5090.csv').replace(DRIVER, '999.99.99')
    kernel = fixture('proc_driver_version.txt').replace(DRIVER, '999.99.99')
    runner = passing_runner(bank, nvidia_smi=smi, kernel_driver=kernel)
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.NVML_DIGEST]
    assert 'unknown driver' in verdict.check(ck.NVML_DIGEST).evidence['reason']
    assert verdict.check(ck.NVML_DIGEST).evidence['driver'] == '999.99.99'


def test_empty_driver_string_fails_closed(bank, allowlist):
    """The Lium bug from `22`: an empty driver string must not skip the digest check."""
    smi = fixture('nvidia_smi_5090.csv').replace(DRIVER, '')
    runner = passing_runner(bank, nvidia_smi=smi).on(
        KERNEL_DRIVER_COMMAND, failing('cat: /proc/driver/nvidia/version: No such file')
    )
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert (
        verdict.failed == [ck.NVML_DIGEST] and verdict.check(ck.NVML_DIGEST).evidence['reason'] == 'empty driver string'
    )


def test_patched_nvml_lib_and_shimmed_nvidia_smi_fail(bank, allowlist):
    runner = passing_runner(bank, nvml_md5='0' * 32 + '  /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1\n')
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.NVML_DIGEST] and verdict.check(ck.NVML_DIGEST).evidence['reason'] == 'digest mismatch'
    runner = passing_runner(bank, kernel_driver=fixture('proc_driver_version.txt').replace(DRIVER, '575.64.03'))
    assert run_full_check(runner, allowlist, bank, config=CONFIG).failed == [ck.NVML_DIGEST]
    runner = passing_runner(bank).on(NVML_MD5_COMMAND, failing(''))
    assert (
        'not found' in run_full_check(runner, allowlist, bank, config=CONFIG).check(ck.NVML_DIGEST).evidence['reason']
    )


def test_low_power_limit_fails(bank, allowlist):
    smi = fixture('nvidia_smi_5090.csv').replace('575.00, 575.00, 600.00', '450.00, 575.00, 600.00')
    verdict = run_full_check(passing_runner(bank, nvidia_smi=smi), allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.POWER_LIMIT]
    ev = verdict.check(ck.POWER_LIMIT).evidence
    assert 'below floor' in ev['reason'] and ev['readings'][0]['ratio'] == round(450 / 575, 4)
    # exactly 90% passes; an unreported limit fails closed
    smi = fixture('nvidia_smi_5090.csv').replace('575.00, 575.00, 600.00', '517.50, 575.00, 600.00')
    assert run_full_check(passing_runner(bank, nvidia_smi=smi), allowlist, bank, config=CONFIG).admitted
    smi = fixture('nvidia_smi_5090.csv').replace('575.00, 575.00, 600.00', '[N/A], [N/A], [N/A]')
    verdict = run_full_check(passing_runner(bank, nvidia_smi=smi), allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.POWER_LIMIT] and verdict.check(ck.POWER_LIMIT).evidence['incomplete'] == [UUID_5090]


def test_bad_gpu_proof_digest_fails(bank, allowlist):
    runner = passing_runner(bank, job=job_responder(bank.bank, digest='f' * 64))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.verdict == BENCH and verdict.failed == [ck.GPU_PROOF]
    assert 'digest mismatch' in verdict.check(ck.GPU_PROOF).evidence['reason']
    assert bank.remaining == 4  # the seed is spent either way


def test_stale_wall_fails_gpu_proof(bank, allowlist):
    runner = passing_runner(bank, job=job_responder(bank.bank, wall_ms=1.6 * 1500.0 + 1))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'too slow' in verdict.check(ck.GPU_PROOF).evidence['reason']
    runner = passing_runner(bank, job=job_responder(bank.bank, wall_ms=1.6 * 1500.0 - 1))
    assert run_full_check(runner, allowlist, bank, config=CONFIG).admitted


def test_our_own_clock_bounds_the_proof(bank, allowlist):
    """A relay to a card elsewhere pays the round trip: the docker run took 20 s by our clock, whatever it typed
    (bank run_ms 2400 x 1.6 + 2000 slack = 5840 ms budget); 5 s is an honest box's container start."""
    from gittensor.controller.checks.scrape import scrape_host

    ticks = iter([0.0, 20.0])
    scrape = scrape_host(passing_runner(bank), network_targets=NETWORK_TARGETS)
    result = ck.check_gpu_proof(
        passing_runner(bank),
        scrape.gpus,
        bank,
        CONFIG.challenge_params,
        CONFIG.challenge_image,
        clock=lambda: next(ticks),
    )
    assert not result.passed and 'too slow: 20000 ms round trip > 5840 ms' in result.evidence['reason']
    assert (
        result.evidence['cards'][0]['outer_budget_ms'] == 5840.0
        and result.evidence['cards'][0]['bank_run_ms'] == 2400.0
    )
    ticks = iter([0.0, 5.0])
    result = ck.check_gpu_proof(
        passing_runner(bank),
        scrape.gpus,
        bank,
        CONFIG.challenge_params,
        CONFIG.challenge_image,
        clock=lambda: next(ticks),
    )
    assert result.passed and result.evidence['cards'][0]['elapsed_ms'] == 5000.0


def test_proof_answered_by_another_card_or_underfilled_fails(bank, allowlist):
    runner = passing_runner(bank, job=job_responder(bank.bank, uuid='GPU-relay-target'))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and f'not {UUID_5090}' in verdict.check(ck.GPU_PROOF).evidence['reason']
    runner = passing_runner(bank, job=job_responder(bank.bank, filled_bytes=8_000_000_000))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'under-filled' in verdict.check(ck.GPU_PROOF).evidence['reason']


def test_proof_job_failure_and_dead_transport(bank, allowlist):
    runner = passing_runner(bank).on(
        regex(r'^docker run '), failing('{"device":0,"error":"not enough free VRAM: 1234"}')
    )
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'job error' in verdict.check(ck.GPU_PROOF).evidence['reason']
    runner = passing_runner(bank).on(regex(r'^docker run '), TimeoutError('ssh: timed out'))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF] and 'TimeoutError' in verdict.check(ck.GPU_PROOF).evidence['reason']


def test_bank_depletion_is_reported(allowlist, tmp_path):
    from gittensor.controller.challenge.bank import BankConsumer

    bank = BankConsumer(make_bank(1), tmp_path / 'used.json')
    runner = passing_runner(bank, nvidia_smi=fixture('nvidia_smi_2x5090.csv'))
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_PROOF]
    ev = verdict.check(ck.GPU_PROOF).evidence
    assert 'depleted' in ev['reason'] and ev['bank']['depleted'] is True and ev['cards'][0]['passed'] is True


def test_agent_image_digest_disk_and_network(bank, allowlist):
    runner = passing_runner(bank, agent_image='ghcr.io/entrius/gt-agent@sha256:' + 'e' * 64 + '\n')
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert (
        verdict.failed == [ck.AGENT_IMAGE]
        and 'not one we published' in verdict.check(ck.AGENT_IMAGE).evidence['reason']
    )
    runner = passing_runner(bank).on(agent_image_command(), failing('Error: No such object: gittensor-agent'))
    assert run_full_check(runner, allowlist, bank, config=CONFIG).failed == [ck.AGENT_IMAGE]
    # no digest pinned in config -> fail closed
    unpinned = FullCheckConfig(agent_image_digests=(), network_targets=NETWORK_TARGETS)
    assert run_full_check(passing_runner(bank), allowlist, bank, config=unpinned).failed == [ck.AGENT_IMAGE]
    runner = passing_runner(
        bank, df='Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000000 900000 50000000 90% /\n'
    )
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.DISK_FREE] and '< 100 GB' in verdict.check(ck.DISK_FREE).evidence['reason']
    runner = passing_runner(bank).on(disk_free_command(), failing('df: /var/lib/docker: No such file or directory'))
    assert run_full_check(runner, allowlist, bank, config=CONFIG).failed == [ck.DISK_FREE]
    runner = passing_runner(bank).on(
        network_command(NETWORK_TARGETS[1]), failing('curl: (6) Could not resolve host', 6)
    )
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.NETWORK] and NETWORK_TARGETS[1] in verdict.check(ck.NETWORK).evidence['reason']


def test_several_failures_are_all_named(bank, allowlist):
    smi = fixture('nvidia_smi_4090.csv').replace(DRIVER, '1.2.3')
    runner = passing_runner(bank, nvidia_smi=smi, kernel_driver='NVRM version: Kernel Module 1.2.3\n')
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.failed == [ck.GPU_SPEC, ck.NVML_DIGEST] and verdict.skipped == [ck.GPU_PROOF]


def test_no_gpu_at_all(bank, allowlist):
    runner = passing_runner(bank).on(
        nvidia_smi_command(),
        failing("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.", 9),
    )
    verdict = run_full_check(runner, allowlist, bank, config=CONFIG)
    assert verdict.verdict == BENCH and ck.GPU_SPEC in verdict.failed and ck.POWER_LIMIT in verdict.failed
    assert verdict.gpu_uuids == [] and bank.remaining == 5
    assert 'nvidia-smi' in verdict.check(ck.GPU_SPEC).evidence['reason']
