# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The scrape parsers against recorded nvidia-smi / df / docker / curl output."""

import pytest

from gittensor.controller.checks.runner import FakeRunner
from gittensor.controller.checks.scrape import (
    NVIDIA_SMI_FIELDS,
    agent_image_command,
    network_command,
    nvidia_smi_command,
    parse_curl,
    parse_df_available_gb,
    parse_kernel_driver,
    parse_md5,
    parse_nvidia_smi,
    parse_repo_digests,
    scrape_host,
)
from tests.controller.conftest import DRIVER, NVML_MD5, UUID_5090, UUID_5090_B, fixture, passing_runner


def test_nvidia_smi_command_names_every_field():
    cmd = nvidia_smi_command()
    assert cmd.startswith('nvidia-smi --query-gpu=') and '--format=csv,noheader,nounits' in cmd
    for f in NVIDIA_SMI_FIELDS:
        assert f in cmd


def test_parse_recorded_5090():
    (gpu,) = parse_nvidia_smi(fixture('nvidia_smi_5090.csv'))
    assert gpu.uuid == UUID_5090 and gpu.name == 'NVIDIA GeForce RTX 5090' and gpu.driver == DRIVER
    assert gpu.memory_total_mib == 32607 and gpu.memory_total_bytes == 32607 * 1024 * 1024
    assert gpu.power_limit_w == 575.0 and gpu.power_default_limit_w == 575.0 and gpu.power_max_limit_w == 600.0
    assert gpu.pci_bus_id == '00000000:01:00.0' and gpu.compute_cap == '12.0'


def test_parse_two_cards_and_na_fields():
    gpus = parse_nvidia_smi(fixture('nvidia_smi_2x5090.csv'))
    assert [g.uuid for g in gpus] == [UUID_5090, UUID_5090_B]
    (gpu,) = parse_nvidia_smi(
        'GPU-x, NVIDIA GeForce RTX 5090, 580.65.06, 32607, [N/A], [N/A], [N/A], 00000000:01:00.0, 12.0'
    )
    assert gpu.power_limit_w is None and gpu.power_default_limit_w is None


def test_parse_wrong_column_count_raises():
    with pytest.raises(ValueError):
        parse_nvidia_smi('GPU-x, NVIDIA GeForce RTX 5090, 580.65.06\n')


def test_parse_md5_and_kernel_driver():
    assert parse_md5(f'{NVML_MD5}  /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.580.65.06\n') == NVML_MD5
    assert parse_md5('md5sum: no such file') == ''
    assert parse_kernel_driver(fixture('proc_driver_version.txt')) == DRIVER
    assert parse_kernel_driver('') == ''


def test_parse_repo_digests_df_and_curl():
    out = 'ghcr.io/entrius/gt-agent@sha256:' + 'a' * 64 + ',docker.io/x/y@sha256:' + 'b' * 64 + '\n'
    assert parse_repo_digests(out) == ['sha256:' + 'a' * 64, 'sha256:' + 'b' * 64]
    assert parse_repo_digests('') == []
    free = parse_df_available_gb(fixture('df_docker.txt'))
    assert free is not None and abs(free - 1311634392 * 1024 / 1e9) < 1e-6
    assert parse_df_available_gb('') is None
    assert parse_curl('200 4812345.000') == (200, 4812345.0)
    assert parse_curl('') == (0, 0.0)


def test_commands_quote_arguments():
    assert "'gt-agent'" in agent_image_command() or 'gt-agent' in agent_image_command()
    assert 'https://x.example/a' in network_command('https://x.example/a') and '-m 10' in network_command(
        'https://x.example/a'
    )


def test_scrape_host_collects_everything_and_records_errors(tmp_path):
    scrape = scrape_host(passing_runner(), network_targets=('https://registry.example/v2/', 'https://hub.example/api'))
    assert scrape.uuids == [UUID_5090] and scrape.driver == DRIVER and scrape.kernel_driver == DRIVER
    assert scrape.nvml_md5 == NVML_MD5 and scrape.nvml_path.endswith('libnvidia-ml.so.580.65.06')
    assert scrape.agent_image_digests == ['sha256:' + 'a' * 64]
    assert scrape.disk_free_gb and scrape.disk_free_gb > 1000
    assert scrape.network['https://registry.example/v2/'] == (200, 4812345.0)
    assert scrape.errors == {}
    # a dead transport on one step fails that step only
    dead = FakeRunner().on(nvidia_smi_command(), ConnectionError('ssh: connection reset'))
    scrape = scrape_host(dead, network_targets=())
    assert scrape.gpus == [] and 'ConnectionError' in scrape.errors['nvidia_smi']
    assert 'no response' in scrape.errors['nvml_md5']
