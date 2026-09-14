# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The challenge bank: generation from a job runner, once-only checkout with persistence, depletion, judging."""

import json

import pytest

from gittensor.controller.challenge import bank as bank_mod
from gittensor.controller.challenge.bank import (
    BankConsumer,
    BankDepleted,
    BankEntry,
    ChallengeBank,
    ChallengeParams,
    generate_bank,
    image_ref,
    job_command,
    job_command_str,
    judge_answer,
    random_seeds,
    timing_summary,
)
from tests.controller.conftest import (
    FILLED_BYTES,
    PARAMS,
    UUID_5090,
    VRAM_TOTAL_BYTES,
    digest_for,
    job_responder,
    make_bank,
)


def fake_job(seed: int) -> dict:
    """What run_local_job returns for an honest card."""
    return json.loads(job_responder(make_bank(0))(f'docker run --gpus="device={UUID_5090}" img --seed {seed}')) | {
        'digest': digest_for(seed),
        'wall_ms': 1500.0 + (seed % 7),
    }


def test_generate_bank_collects_entries_and_skips_errors(tmp_path, capsys):
    seeds = [11, 22, 33, 44]

    def run_job(seed):
        return {'error': 'not enough free VRAM'} if seed == 33 else fake_job(seed)

    bank = generate_bank(run_job, seeds, PARAMS, image_digest='sha256:' + 'c' * 64, now=1234.0)
    assert bank.seeds == [11, 22, 44] and bank.card_name == 'NVIDIA GeForce RTX 5090' and bank.generated_at == 1234.0
    assert all(e.digest == digest_for(e.seed) and e.run_ms == 2400.0 for e in bank.entries)
    assert 'seed 33: skipped' in capsys.readouterr().err
    bank.save(tmp_path / 'bank.json')
    loaded = ChallengeBank.load(tmp_path / 'bank.json')
    assert loaded.as_dict() == bank.as_dict() and loaded.params == PARAMS


def test_generate_bank_refuses_other_parameters():
    with pytest.raises(ValueError):
        generate_bank(lambda s: fake_job(s) | {'iters': 5}, [1], PARAMS)


def test_duplicate_seeds_rejected_and_random_seeds_unique():
    with pytest.raises(ValueError):
        ChallengeBank(PARAMS, make_bank(2).entries + make_bank(1).entries)
    seeds = random_seeds(50)
    assert len(set(seeds)) == 50 and all(0 <= s < 2**62 for s in seeds)


def test_consumer_hands_out_each_seed_once_and_persists(tmp_path):
    used = tmp_path / 'bank.used.json'
    consumer = BankConsumer(make_bank(3), used, low_water=1)
    assert consumer.remaining == 3 and not consumer.low and not consumer.depleted
    first = consumer.checkout()
    assert first.seed == 1000 and json.loads(used.read_text()) == [1000]  # persisted before it is returned
    # reopening the same pair resumes: 1000 is never handed out again
    reopened = BankConsumer(make_bank(3), used, low_water=1)
    assert reopened.remaining == 2 and reopened.checkout().seed == 1001
    assert reopened.low is True and reopened.remaining == 1
    assert reopened.checkout().seed == 1002
    assert reopened.depleted and reopened.status()['remaining'] == 0
    with pytest.raises(BankDepleted):
        reopened.checkout()
    assert sorted(json.loads(used.read_text())) == [1000, 1001, 1002]


def test_consumer_open_from_files(tmp_path):
    make_bank(2).save(tmp_path / 'bank.json')
    consumer = BankConsumer.open(tmp_path / 'bank.json')
    assert consumer.used_path == tmp_path / 'bank.used.json'
    consumer.checkout()
    assert BankConsumer.open(tmp_path / 'bank.json').remaining == 1


def test_judge_answer_happy_path_and_each_failure():
    bank = make_bank(1)
    entry = bank.entries[0]
    honest = json.loads(job_responder(bank)(f'docker run --gpus="device={UUID_5090}" img --seed {entry.seed}'))
    ok = judge_answer(entry, honest, PARAMS, expected_uuid=UUID_5090, vram_total_bytes=VRAM_TOTAL_BYTES)
    assert ok.passed and ok.reason == 'ok' and ok.budget_ms == 1.6 * 1500.0
    assert judge_answer(entry, honest | {'digest': 'f' * 64}, PARAMS).reason == 'digest mismatch'
    assert 'too slow' in judge_answer(entry, honest | {'wall_ms': 2401.0}, PARAMS).reason
    assert judge_answer(entry, honest | {'wall_ms': 2399.0}, PARAMS).passed
    # our outer clock is judged against the bank's run_ms (2400) x 1.6 + 2000 slack = 5840, not the inner wall
    outer = judge_answer(entry, honest, PARAMS, elapsed_ms=5000.0)
    assert outer.passed and outer.outer_budget_ms == 1.6 * 2400.0 + 2000.0 and outer.elapsed_ms == 5000.0
    assert 'round trip' in judge_answer(entry, honest, PARAMS, elapsed_ms=5841.0).reason
    assert judge_answer(entry, honest, PARAMS, elapsed_ms=5841.0, rtt_slack_ms=3000.0).passed
    # a bank without run_ms falls back to its wall: 1500 x 1.6 + 2000 = 4400
    no_run = BankEntry.from_dict(entry.as_dict() | {'run_ms': None})
    assert judge_answer(no_run, honest, PARAMS, elapsed_ms=4400.0).passed
    assert 'round trip' in judge_answer(no_run, honest, PARAMS, elapsed_ms=4401.0).reason
    under = judge_answer(
        entry, honest | {'filled_bytes': int(0.5 * FILLED_BYTES)}, PARAMS, vram_total_bytes=VRAM_TOTAL_BYTES
    )
    assert 'under-filled' in under.reason
    assert 'other job parameters' in judge_answer(entry, honest | {'iters': 1}, PARAMS).reason
    assert 'different seed' in judge_answer(entry, honest | {'seed': 7}, PARAMS).reason
    other = judge_answer(entry, honest, PARAMS, expected_uuid='GPU-somewhere-else')
    assert not other.passed and 'not GPU-somewhere-else' in other.reason
    assert 'job error' in judge_answer(entry, {'error': 'fill'}, PARAMS).reason
    assert 'malformed' in judge_answer(entry, honest | {'wall_ms': 'fast'}, PARAMS).reason
    assert not judge_answer(entry, 'not json', PARAMS).passed  # type: ignore[arg-type]


def test_job_command_pins_one_card_and_asks_for_device_zero():
    cmd = job_command(42, PARAMS, 'ghcr.io/entrius/gt-challenge:dev', UUID_5090)
    assert cmd[:4] == ['docker', 'run', '--rm', f'--gpus="device={UUID_5090}"']
    assert cmd[4] == 'ghcr.io/entrius/gt-challenge:dev'
    assert '--seed' in cmd and cmd[cmd.index('--seed') + 1] == '42'
    assert cmd[-2:] == ['--device', '0'] and '--fill-ratio' in cmd and cmd[cmd.index('--fill-ratio') + 1] == '0.9'
    s = job_command_str(42, PARAMS, 'img', UUID_5090)
    assert s.startswith(f'docker run --rm --gpus="device={UUID_5090}" img --seed 42 --fill-ratio 0.9 --iters 3')
    assert job_command(1, PARAMS, 'img')[3] == '--gpus=all'
    assert image_ref('img', 'dev', '') == 'img:dev' and image_ref('img', 'dev', 'sha256:ab') == 'img@sha256:ab'


def test_params_roundtrip_and_matching():
    p = ChallengeParams(iters=5, fill_ratio=0.5, dim=512, matrices=64)
    assert ChallengeParams.from_dict(p.as_dict()) == p
    assert p.matches({'iters': 5, 'fill_ratio': 0.5, 'dim': 512, 'matrices': 64})
    assert p.matches({})  # a job that reports nothing is taken at the bank's params
    assert not p.matches({'fill_ratio': 0.9}) and not p.matches({'iters': 'x'})


def test_timing_summary_and_status_cli(tmp_path, capsys):
    bank = make_bank(3)
    summary = timing_summary(bank.entries)
    assert summary['wall_ms_median'] == 1500.0 and summary['run_ms_max'] == 2400.0
    assert timing_summary([])['wall_ms_min'] is None
    bank.save(tmp_path / 'bank.json')
    assert bank_mod.main(['status', '--bank', str(tmp_path / 'bank.json')]) == 0
    assert json.loads(capsys.readouterr().out)['remaining'] == 3
