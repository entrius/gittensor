# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt up / gitt down: prerequisite checks against a fake host, --dry-run output, and the docker calls issued."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.up_commands import prereqs
from gittensor.cli.up_commands.prereqs import PrereqReport, check_ports, check_toolkit, run_prereqs

SMI_OK = 'NVIDIA GeForce RTX 5090, 580.65.06, GPU-1111\n'


class FakeProbe:
    """A host with everything installed unless a test says otherwise."""

    def __init__(self):
        self.smi = subprocess.CompletedProcess([], 0, SMI_OK, '')
        self.docker_info = subprocess.CompletedProcess([], 0, '29.7.2\n', '')
        self.runtimes = subprocess.CompletedProcess([], 0, '{"runc":{}}', '')
        self.binaries = {'nvidia-ctk': '/usr/bin/nvidia-ctk'}
        self.busy_ports: set[int] = set()
        self.ss58 = '5FakeMinerHotkeyAddress000000000000000000000000'
        self.registered = True
        self.states: dict[str, str | None] = {'gt-agent': None, 'gt-agent-runner': None}
        self.chain_calls = 0

    def run(self, cmd, timeout=20.0):
        if cmd[0] == 'nvidia-smi':
            return self.smi
        if cmd[:2] == ['docker', 'info']:
            return self.runtimes if 'Runtimes' in cmd[-1] else self.docker_info
        raise AssertionError(f'unexpected command {cmd}')

    def which(self, name):
        return self.binaries.get(name)

    def port_free(self, port):
        return port not in self.busy_ports

    def hotkey_ss58(self, wallet, hotkey, wallet_path=None):
        return self.ss58

    def is_registered(self, ss58, netuid, endpoint):
        self.chain_calls += 1
        return self.registered

    def container_state(self, name):
        return self.states.get(name)


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    # rich soft-wraps at 80 columns under CliRunner; the docker lines are longer than that
    monkeypatch.setenv('COLUMNS', '250')


@pytest.fixture
def probe():
    return FakeProbe()


@pytest.fixture
def docker_calls(probe):
    calls = []

    def _run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, 'abc123\n', '')

    with (
        patch('gittensor.cli.up_commands.up._make_probe', return_value=probe),
        patch('gittensor.cli.up_commands.docker_exec.run_docker', side_effect=_run),
    ):
        yield calls


@pytest.fixture
def runner():
    return CliRunner()


UP = ['up', '--wallet', 'alice', '--hotkey', 'default', '--network', 'test']


class TestPrereqs:
    def test_all_pass(self, probe):
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        assert report.ok and report.hotkey_ss58 == probe.ss58 and not report.already_up
        assert [r.status for r in report.results] == ['pass'] * 6

    def test_no_driver_fails(self, probe):
        probe.smi = subprocess.CompletedProcess([], 127, '', 'nvidia-smi: command not found')
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        assert not report.ok
        assert report.results[0].name == 'NVIDIA driver' and report.results[0].status == 'fail'

    def test_wrong_gpu_is_a_warning_not_a_failure(self, probe):
        probe.smi = subprocess.CompletedProcess([], 0, 'NVIDIA GeForce RTX 4090, 550.1, GPU-9\n', '')
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        warn = [r for r in report.results if r.status == 'warn']
        assert report.ok and len(warn) == 1 and '5090' in warn[0].detail

    def test_docker_down_fails_and_skips_container_lookup(self, probe):
        probe.docker_info = subprocess.CompletedProcess([], 1, '', 'Cannot connect to the Docker daemon')
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        assert not report.ok and report.agent_state is None
        assert any(r.name == 'Docker daemon' and 'Cannot connect' in r.detail for r in report.results)

    def test_toolkit_via_runtime_registration(self, probe):
        probe.binaries = {}
        probe.runtimes = subprocess.CompletedProcess([], 0, '{"nvidia":{"path":"nvidia-container-runtime"}}', '')
        assert check_toolkit(probe).ok
        probe.runtimes = subprocess.CompletedProcess([], 0, '{"runc":{}}', '')
        assert not check_toolkit(probe).ok

    def test_busy_port_fails_unless_ours(self, probe):
        probe.busy_ports = {2200}
        report = PrereqReport()
        result = check_ports(probe, [2200, 8200], report)
        assert not result.ok and '2200' in result.detail
        report.agent_state = 'running'
        assert check_ports(probe, [2200, 8200], report).ok

    def test_missing_hotkey_file_fails_both_hotkey_checks(self, probe):
        probe.ss58 = None
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        assert [r.status for r in report.results[-2:]] == ['fail', 'fail']
        assert probe.chain_calls == 0

    def test_unregistered_fails(self, probe):
        probe.registered = False
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        assert not report.ok and 'not registered' in report.results[-1].detail

    def test_chain_error_is_reported_not_raised(self, probe):
        def boom(*a):
            raise ConnectionError('rpc down')

        probe.is_registered = boom
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200)
        assert not report.ok and 'rpc down' in report.results[-1].detail

    def test_skip_chain(self, probe):
        report = run_prereqs(
            probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, http_port=8200, skip_chain=True
        )
        assert report.ok and report.results[-1].status == 'skip' and probe.chain_calls == 0

    def test_real_probe_reads_hotkey_file(self, tmp_path):
        (tmp_path / 'w' / 'hotkeys').mkdir(parents=True)
        (tmp_path / 'w' / 'hotkeys' / 'h').write_text(json.dumps({'ss58Address': '5Real', 'publicKey': '0x'}))
        real = prereqs.HostProbe()
        assert real.hotkey_ss58('w', 'h', wallet_path=tmp_path) == '5Real'
        assert real.hotkey_ss58('w', 'missing', wallet_path=tmp_path) is None

    def test_real_probe_port_free(self):
        import socket

        real = prereqs.HostProbe()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen()
            port = s.getsockname()[1]
            assert real.port_free(port) is False
        assert real.port_free(port) is True


class TestUpCommand:
    def test_help(self, runner):
        result = runner.invoke(cli, ['up', '--help'])
        assert result.exit_code == 0 and 'compute agent' in result.output

    def test_dry_run_prints_table_and_commands_without_docker(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--dry-run'])
        assert result.exit_code == 0, result.output
        out = result.output
        assert 'prerequisites' in out and 'NVIDIA driver' in out and 'skipped (--dry-run)' in out
        assert 'Would run:' in out
        assert 'docker run -d --name gt-agent-runner' in out
        assert 'docker run -d --name gt-agent --restart unless-stopped --privileged --pid host --gpus all' in out
        assert probe.ss58 in out
        assert docker_calls == [] and probe.chain_calls == 0

    def test_dry_run_survives_failed_checks(self, runner, docker_calls, probe):
        probe.docker_info = subprocess.CompletedProcess([], 1, '', 'no daemon')
        result = runner.invoke(cli, [*UP, '--dry-run'])
        assert result.exit_code == 0 and 'fail' in result.output and 'Would run:' in result.output

    def test_failed_prereq_exits_1_without_docker(self, runner, docker_calls, probe):
        probe.registered = False
        result = runner.invoke(cli, UP)
        assert result.exit_code == 1
        assert 'Prerequisites failed' in result.output and docker_calls == []

    def test_happy_path_starts_the_runner(self, runner, docker_calls, probe):
        result = runner.invoke(cli, UP)
        assert result.exit_code == 0, result.output
        assert len(docker_calls) == 1
        cmd = docker_calls[0]
        assert cmd[:5] == ['docker', 'run', '-d', '--name', 'gt-agent-runner']
        assert f'GT_AGENT_MINER_HOTKEY={probe.ss58}' in cmd
        assert 'Started gt-agent-runner' in result.output

    def test_no_update_starts_the_agent_directly(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--no-update', '--image', 'local/gt-agent:dev', '--ssh-port', '2201'])
        assert result.exit_code == 0, result.output
        (cmd,) = docker_calls
        assert cmd[:5] == ['docker', 'run', '-d', '--name', 'gt-agent'] and cmd[-1] == 'local/gt-agent:dev'
        assert '-p' in cmd and '2201:2201' in cmd and '--privileged' in cmd

    def test_stopped_runner_is_removed_before_start(self, runner, docker_calls, probe):
        probe.states['gt-agent-runner'] = 'exited'
        result = runner.invoke(cli, UP)
        assert result.exit_code == 0, result.output
        assert docker_calls[0] == ['docker', 'rm', '-f', 'gt-agent-runner'] and docker_calls[1][1] == 'run'

    def test_already_up_is_a_noop(self, runner, docker_calls, probe):
        probe.states['gt-agent-runner'] = 'running'
        probe.states['gt-agent'] = 'running'
        probe.busy_ports = {2200, 8200}  # held by our own containers
        result = runner.invoke(cli, UP)
        assert result.exit_code == 0 and 'Already up' in result.output and docker_calls == []

    def test_docker_run_failure_exits_1(self, runner, probe):
        with (
            patch('gittensor.cli.up_commands.up._make_probe', return_value=probe),
            patch(
                'gittensor.cli.up_commands.docker_exec.run_docker',
                return_value=subprocess.CompletedProcess([], 125, '', 'port is already allocated'),
            ),
        ):
            result = runner.invoke(cli, UP)
        assert result.exit_code == 1 and 'port is already allocated' in result.output

    def test_json_dry_run_envelope(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--dry-run', '--json'])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload['success'] and payload['dry_run'] and payload['hotkey_ss58'] == probe.ss58
        assert {c['name'] for c in payload['checks']} >= {'NVIDIA driver', 'Docker daemon', 'Ports free'}
        assert payload['commands'][0].startswith('docker run -d --name gt-agent-runner')
        assert '--privileged' in payload['agent_command']

    def test_json_failure_envelope(self, runner, docker_calls, probe):
        probe.ss58 = None
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 1
        # the envelope (pretty) followed by the compact error envelope: two JSON documents on stdout
        decoder, text, docs = json.JSONDecoder(), result.stdout.strip(), []
        while text:
            doc, end = decoder.raw_decode(text)
            docs.append(doc)
            text = text[end:].lstrip()
        assert len(docs) == 2 and all(d['success'] is False for d in docs)
        assert docs[0]['checks'][-2]['status'] == 'fail'


class TestDownCommand:
    def test_dry_run(self, runner, docker_calls):
        result = runner.invoke(cli, ['down', '--dry-run'])
        assert result.exit_code == 0
        assert 'docker rm -f gt-agent-runner' in result.output and 'docker rm -f gt-agent' in result.output
        assert docker_calls == []

    def test_removes_both(self, runner, docker_calls):
        result = runner.invoke(cli, ['down'])
        assert result.exit_code == 0
        assert docker_calls == [['docker', 'rm', '-f', 'gt-agent-runner'], ['docker', 'rm', '-f', 'gt-agent']]
        assert result.output.count('Removed') == 2

    def test_missing_containers_are_reported_quietly(self, runner, probe):
        missing = subprocess.CompletedProcess([], 1, '', 'Error response from daemon: No such container: gt-agent')
        with patch('gittensor.cli.up_commands.docker_exec.run_docker', return_value=missing):
            result = runner.invoke(cli, ['down', '--json'])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [c['removed'] for c in payload['containers']] == [False, False]


def test_docker_assets_exist():
    root = Path(__file__).resolve().parents[2] / 'docker' / 'agent'
    for name in ('Dockerfile', 'runner.Dockerfile', 'entrypoint.sh', 'runner.sh', 'sshd.conf'):
        assert (root / name).is_file(), name
