# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt up / gitt down: prerequisite checks against a fake host, --dry-run output, the docker calls issued, the clean
leave (`down` removes the agent first, then drains and removes our workloads) and `up --reclaim` over an orphan of
ours."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.agent.channel import Channel, ChannelError
from gittensor.agent.launch import Workload, parse_workloads, workload_list_command
from gittensor.cli.main import cli
from gittensor.cli.up_commands import prereqs
from gittensor.cli.up_commands.prereqs import PrereqReport, check_ports, check_toolkit, run_prereqs

SMI_OK = 'NVIDIA GeForce RTX 5090, 580.65.06, GPU-1111\n'
AGENT_REF = 'entrius/gt-agent@sha256:' + 'a' * 64
RUNNER_REF = 'entrius/gt-agent-runner@sha256:' + 'b' * 64
CHANNEL = Channel(AGENT_REF, RUNNER_REF, '5.1.0', 1_789_000_000)


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
        self.ip = '44.10.0.1'
        self.answers = True  # the sshd port answers on the public IP
        self.on_chain: tuple | None = None  # (ip, port, compute marker) the chain holds for the hotkey
        self.served: list[tuple] = []
        self.serve_error = ''
        self.workloads: list[Workload] = []  # the controller's gt-i-* containers present on the box

    def ps_output(self) -> str:
        """What `docker ps` prints for our workloads (the format `workload_list_command` asks for)."""
        return ''.join(
            f'{w.container_id}\t{w.name}\t{w.state}\t{w.port or ""}\t{"" if w.drain_max_s is None else w.drain_max_s}\n'
            for w in self.workloads
        )

    def public_ip(self):
        return self.ip

    def reachable(self, ip, port):
        return self.answers

    def chain_endpoint(self, ss58, netuid, endpoint):
        self.chain_calls += 1
        return self.on_chain

    def serve(self, wallet, hotkey, netuid, endpoint, ip, port):
        self.chain_calls += 1
        if self.serve_error:
            return self.serve_error
        self.served.append((wallet, hotkey, netuid, ip, port))
        self.on_chain = (ip, port, True)
        return ''

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

    def workload_containers(self):
        return list(self.workloads)


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
        if cmd[:2] == ['docker', 'ps']:  # `gitt down` lists our workloads through the same seam
            return subprocess.CompletedProcess(cmd, 0, probe.ps_output(), '')
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, 'abc123\n', '')

    with (
        patch('gittensor.cli.up_commands.up._make_probe', return_value=probe),
        patch('gittensor.cli.up_commands.up._load_channel', return_value=CHANNEL),
        patch('gittensor.cli.up_commands.docker_exec.run_docker', side_effect=_run),
    ):
        yield calls


@pytest.fixture
def runner():
    return CliRunner()


UP = ['up', '--wallet', 'alice', '--hotkey', 'default', '--network', 'test']


class TestPrereqs:
    def test_all_pass(self, probe):
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
        assert report.ok and report.hotkey_ss58 == probe.ss58 and not report.already_up
        assert [r.status for r in report.results] == ['pass'] * 9
        assert [r.name for r in report.results][4:7] == ['Workload ports', 'Public IP', 'SSH port reachable']
        assert report.public_ip == probe.ip and probe.chain_calls == 1  # the registration lookup only

    def test_no_driver_fails(self, probe):
        probe.smi = subprocess.CompletedProcess([], 127, '', 'nvidia-smi: command not found')
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
        assert not report.ok
        assert report.results[0].name == 'NVIDIA driver' and report.results[0].status == 'fail'

    def test_wrong_gpu_is_a_warning_not_a_failure(self, probe):
        probe.smi = subprocess.CompletedProcess([], 0, 'NVIDIA GeForce RTX 4090, 550.1, GPU-9\n', '')
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
        warn = [r for r in report.results if r.status == 'warn']
        assert report.ok and len(warn) == 1 and '5090' in warn[0].detail

    def test_docker_down_fails_and_skips_container_lookup(self, probe):
        probe.docker_info = subprocess.CompletedProcess([], 1, '', 'Cannot connect to the Docker daemon')
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
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
        result = check_ports(probe, [2200], report)
        assert not result.ok and '2200' in result.detail
        report.agent_state = 'running'
        assert check_ports(probe, [2200], report).ok

    def test_missing_hotkey_file_fails_both_hotkey_checks(self, probe):
        probe.ss58 = None
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
        assert [r.status for r in report.results[-2:]] == ['fail', 'fail']
        assert probe.chain_calls == 0

    def test_unregistered_fails(self, probe):
        probe.registered = False
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
        assert not report.ok and 'not registered' in report.results[-1].detail

    def test_chain_error_is_reported_not_raised(self, probe):
        def boom(*a):
            raise ConnectionError('rpc down')

        probe.is_registered = boom
        report = run_prereqs(probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200)
        assert not report.ok and 'rpc down' in report.results[-1].detail

    def test_skip_chain(self, probe):
        report = run_prereqs(
            probe, wallet='a', hotkey='h', netuid=74, endpoint='ws://x', ssh_port=2200, skip_chain=True
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
        assert 'docker run -d --name gt-agent-runner' in out and RUNNER_REF in out
        assert 'docker run -d --name gt-agent --restart unless-stopped --privileged --pid host --gpus all' in out
        assert AGENT_REF in out and 'Release channel' in out and '5.1.0' in out
        assert probe.ss58 in out
        assert docker_calls == [] and probe.chain_calls == 0

    def test_unverifiable_channel_fails_and_starts_nothing(self, runner, docker_calls, probe):
        with patch('gittensor.cli.up_commands.up._load_channel', side_effect=ChannelError('signature does not verify')):
            result = runner.invoke(cli, UP)
        assert result.exit_code == 1 and 'signature does not verify' in result.output and docker_calls == []
        with patch('gittensor.cli.up_commands.up._load_channel', side_effect=ChannelError('no release public key')):
            result = runner.invoke(cli, [*UP, '--dry-run'])
        assert result.exit_code == 0 and 'No channel' in result.output and 'Would run' not in result.output

    def test_no_update_never_touches_the_channel(self, runner, docker_calls, probe):
        with patch('gittensor.cli.up_commands.up._load_channel', side_effect=AssertionError('must not be called')):
            result = runner.invoke(cli, [*UP, '--no-update', '--allow-dev-keys', '--image', 'entrius/gt-agent:dev'])
        assert result.exit_code == 0, result.output
        (cmd,) = docker_calls
        assert 'GT_AGENT_ALLOW_DEV_KEYS=1' in cmd and cmd[-1] == 'entrius/gt-agent:dev'

    def test_allow_dev_keys_requires_no_update(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--allow-dev-keys'])
        assert result.exit_code == 2 and 'only applies to --no-update' in result.output and docker_calls == []

    def test_no_chain_dev_box_skips_the_lookup_and_needs_no_hotkey(self, runner, docker_calls, probe):
        probe.ss58 = None
        result = runner.invoke(
            cli, [*UP, '--no-update', '--allow-dev-keys', '--no-chain', '--image', 'entrius/gt-agent:dev']
        )
        assert result.exit_code == 0, result.output
        assert 'WARNING: --no-chain' in result.output and 'NOT a registered miner' in result.output
        assert probe.chain_calls == 0 and len(docker_calls) == 1 and 'GT_AGENT_MINER_HOTKEY=' in docker_calls[0]

    def test_no_chain_requires_no_update(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--no-chain'])
        assert result.exit_code == 2 and 'only applies to --no-update' in result.output and docker_calls == []

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
        assert cmd[:5] == ['docker', 'run', '-d', '--name', 'gt-agent-runner'] and cmd[-1] == RUNNER_REF
        assert f'GT_AGENT_MINER_HOTKEY={probe.ss58}' in cmd
        assert 'Started gt-agent-runner' in result.output

    def test_no_update_starts_the_agent_directly(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--no-update', '--image', 'local/gt-agent:dev', '--ssh-port', '2201'])
        assert result.exit_code == 0, result.output
        (cmd,) = docker_calls
        assert cmd[:5] == ['docker', 'run', '-d', '--name', 'gt-agent'] and cmd[-1] == 'local/gt-agent:dev'
        assert '-p' in cmd and '2201:2201' in cmd and '--privileged' in cmd and 'GT_AGENT_ALLOW_DEV_KEYS=1' not in cmd

    def test_stopped_runner_is_removed_before_start(self, runner, docker_calls, probe):
        probe.states['gt-agent-runner'] = 'exited'
        result = runner.invoke(cli, UP)
        assert result.exit_code == 0, result.output
        assert docker_calls[0] == ['docker', 'rm', '-f', 'gt-agent-runner'] and docker_calls[1][1] == 'run'

    def test_already_up_is_a_noop(self, runner, docker_calls, probe):
        probe.states['gt-agent-runner'] = 'running'
        probe.states['gt-agent'] = 'running'
        probe.busy_ports = {2200}  # held by our own container
        result = runner.invoke(cli, UP)
        assert result.exit_code == 0 and 'Already up' in result.output and docker_calls == []

    def test_a_port_held_by_our_own_orphan_is_named_not_refused_and_reclaimed_with_the_flag(
        self, runner, docker_calls, probe
    ):
        probe.workloads, probe.busy_ports = [ORPHAN], {20000}  # 9/16: the returning `gitt up` was refused on it
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 0, result.output
        row = _json_checks(result)['Workload ports']
        assert row['status'] == 'warn' and 'gt-i-6f31220812a5' in row['detail'] and '--reclaim' in row['detail']
        assert docker_calls == [] or docker_calls[0][1] == 'run'  # nothing of ours touched without the flag
        assert len(docker_calls) == 1 and json.loads(result.stdout)['reclaimed'] == []

        docker_calls.clear()
        result = runner.invoke(cli, [*UP, '--reclaim', '--json'])
        assert result.exit_code == 0, result.output
        row = _json_checks(result)['Workload ports']
        assert row['status'] == 'pass' and 'removed by --reclaim' in row['detail']
        assert docker_calls[:2] == [
            ['docker', 'stop', '--time', '240', ORPHAN.container_id],
            ['docker', 'rm', '-f', ORPHAN.container_id],
        ] and docker_calls[2][1] == 'run'  # fmt: skip
        assert json.loads(result.stdout)['reclaimed'] == ['gt-i-6f31220812a5']

        docker_calls.clear()
        probe.busy_ports = {20000, 20003}  # 20003 is somebody else's: still a refusal, naming only that port
        result = runner.invoke(cli, [*UP, '--reclaim', '--json'])
        assert result.exit_code == 1 and docker_calls == []
        detail = _json_checks(result)['Workload ports']['detail']
        assert 'in use: 20003 (' in detail  # 20000, ours, is not what refuses it

    def test_docker_run_failure_exits_1(self, runner, probe):
        with (
            patch('gittensor.cli.up_commands.up._make_probe', return_value=probe),
            patch('gittensor.cli.up_commands.up._load_channel', return_value=CHANNEL),
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
        assert '--privileged' in payload['agent_command'] and AGENT_REF in payload['agent_command']
        assert payload['channel']['agent'] == AGENT_REF

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


ORPHAN = Workload('a' * 64, 'gt-i-6f31220812a5', 'running', 20000, 240)  # the 27B the 9/16 `gitt down` left serving
STOPPED = Workload('b' * 64, 'gt-i-0123456789ab', 'exited', 20001, 30)
UNLABELLED = Workload('c' * 64, 'gt-i-before0label', 'running', 20002, None)  # started before the drain label existed


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

    def test_the_agent_goes_first_then_our_workloads_are_drained_and_removed(self, runner, docker_calls, probe):
        # the agent before the workloads: the controller sees "unreachable" then "gone after unreachable" (a stop, no
        # bench), never a container gone under a live agent
        probe.workloads = [ORPHAN, STOPPED, UNLABELLED]
        result = runner.invoke(cli, ['down', '--json'])
        assert result.exit_code == 0, result.output
        assert docker_calls == [
            ['docker', 'rm', '-f', 'gt-agent-runner'],
            ['docker', 'rm', '-f', 'gt-agent'],
            ['docker', 'stop', '--time', '240', ORPHAN.container_id],  # SIGTERM, the manifest's drain.max_s
            ['docker', 'stop', '--time', '30', UNLABELLED.container_id],  # no label: the 30 s default; STOPPED: no stop
            ['docker', 'rm', '-f', ORPHAN.container_id],
            ['docker', 'rm', '-f', STOPPED.container_id],
            ['docker', 'rm', '-f', UNLABELLED.container_id],
        ]
        payload = json.loads(result.stdout)
        assert payload['workloads'] == [ORPHAN.name, STOPPED.name, UNLABELLED.name] and payload['list_error'] == ''
        assert [(c['container'], c['action'], c['ok']) for c in payload['containers']][:4] == [
            ('gt-agent-runner', 'rm', True), ('gt-agent', 'rm', True), (ORPHAN.name, 'stop', True), (UNLABELLED.name, 'stop', True),
        ]  # fmt: skip

        docker_calls.clear()
        result = runner.invoke(cli, ['down', '--now'])  # no wait: rm -f at once
        assert result.exit_code == 0 and not any(c[1] == 'stop' for c in docker_calls)
        assert [c[-1] for c in docker_calls] == ['gt-agent-runner', 'gt-agent', ORPHAN.container_id, STOPPED.container_id, UNLABELLED.container_id]  # fmt: skip
        assert 'Drained' not in result.output and result.output.count('Removed') == 5

        docker_calls.clear()
        result = runner.invoke(cli, ['down', '--dry-run'])
        assert docker_calls == [] and f'docker stop --time 240 {ORPHAN.container_id}' in result.output
        assert result.output.index('docker rm -f gt-agent') < result.output.index('docker stop')

    def test_missing_containers_are_reported_quietly(self, runner, probe):
        missing = subprocess.CompletedProcess([], 1, '', 'Error response from daemon: No such container: gt-agent')
        with patch('gittensor.cli.up_commands.docker_exec.run_docker', return_value=missing):
            result = runner.invoke(cli, ['down', '--json'])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [c['ok'] for c in payload['containers']] == [False, False] and 'No such container' in payload[
            'list_error'
        ]


def test_workload_listing_round_trips_the_labels():
    cmd = workload_list_command()
    assert cmd[:3] == ['docker', 'ps', '-a'] and 'label=io.gittensor.instance' in cmd
    assert 'io.gittensor.drain_max_s' in cmd[-1] and 'io.gittensor.port' in cmd[-1]
    out = f'{ORPHAN.container_id}\t{ORPHAN.name}\trunning\t20000\t240\n{"c" * 64}\tgt-i-x\texited\t\t\nnot a row\n'
    assert parse_workloads(out) == [ORPHAN, Workload('c' * 64, 'gt-i-x', 'exited', None, None)]


def test_docker_assets_exist():
    root = Path(__file__).resolve().parents[2] / 'docker' / 'agent'
    for name in (
        'Dockerfile',
        'runner.Dockerfile',
        'entrypoint.sh',
        'runner.sh',
        'sshd.conf',
        'keys/make-dev-keys.sh',
        'keys/README.md',
        'channel/sign.sh',
        'channel/README.md',
    ):
        assert (root / name).is_file(), name


def _json_checks(result):
    envelope, _ = json.JSONDecoder().raw_decode(result.stdout)  # a failure adds the error envelope after it
    return {c['name']: c for c in envelope['checks']}


class TestPublish:
    def test_help_names_what_to_open(self, runner):
        result = runner.invoke(cli, ['up', '--help'])
        assert 'the sshd port (--ssh-port, default 2200)' in result.output and '20000-20015' in result.output

    def test_happy_path_publishes_the_endpoint_then_starts(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 0, result.output
        assert probe.served == [('alice', 'default', 74, '44.10.0.1', 2200)] and len(docker_calls) == 1
        payload = json.loads(result.stdout)
        assert payload['endpoint'] == {
            'ip': '44.10.0.1',
            'port': 2200,
            'netuid': 74,
            'workload_ports': [20000, 20015],
            'published': 'served',
        }
        assert _json_checks(result)['Endpoint published']['detail'] == 'served 44.10.0.1:2200 on netuid 74'

    def test_an_unchanged_endpoint_is_not_served_again(self, runner, docker_calls, probe):
        probe.on_chain = ('44.10.0.1', 2200, True)
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 0 and probe.served == []
        assert json.loads(result.stdout)['endpoint']['published'] == 'unchanged'

    def test_a_changed_ip_or_port_re_serves_even_when_already_up(self, runner, docker_calls, probe):
        probe.states = {'gt-agent': 'running', 'gt-agent-runner': 'running'}
        probe.busy_ports = {2200, 2201, *range(20000, 20004)}  # our sshd and our instances
        probe.on_chain = ('44.10.0.9', 2200, True)
        result = runner.invoke(cli, UP)
        assert result.exit_code == 0, result.output
        assert probe.served[-1][3:] == ('44.10.0.1', 2200) and 'was 44.10.0.9:2200' in result.output
        assert 'Already up' in result.output and docker_calls == []
        runner.invoke(cli, [*UP, '--ssh-port', '2201'])
        assert probe.served[-1][3:] == ('44.10.0.1', 2201) and len(probe.served) == 2
        probe.on_chain = ('44.10.0.1', 2201, False)  # the right address without the marker: a plain axon, re-served
        runner.invoke(cli, [*UP, '--ssh-port', '2201'])
        assert len(probe.served) == 3

    def test_ip_override_and_a_private_ip(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--ip', '52.20.0.3'])
        assert result.exit_code == 0, result.output
        assert probe.served[0][3] == '52.20.0.3' and '52.20.0.3 (--ip)' in result.output
        private = runner.invoke(cli, [*UP, '--ip', '192.168.1.5', '--json'])
        assert private.exit_code == 1 and 'not public' in _json_checks(private)['Public IP']['detail']
        assert len(probe.served) == 1 and len(docker_calls) == 1
        probe.ip = None
        undetected = runner.invoke(cli, [*UP, '--json'])
        assert undetected.exit_code == 1 and 'pass --ip' in _json_checks(undetected)['Public IP']['detail']

    def test_a_failed_serve_starts_nothing(self, runner, docker_calls, probe):
        probe.serve_error = 'ServingRateLimitExceeded'
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 1 and docker_calls == []
        detail = _json_checks(result)['Endpoint published']
        assert detail['status'] == 'fail' and 'ServingRateLimitExceeded' in detail['detail']

    def test_failed_prereqs_publish_nothing(self, runner, docker_calls, probe):
        probe.busy_ports = {20003}
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 1 and probe.served == [] and docker_calls == []
        assert '20003' in _json_checks(result)['Workload ports']['detail']

    def test_dry_run_prints_what_it_would_publish_and_touches_no_chain(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--dry-run', '--json'])
        assert result.exit_code == 0, result.output
        row = _json_checks(result)['Endpoint published']
        assert row['status'] == 'skip' and row['detail'] == 'would publish 44.10.0.1:2200 on netuid 74'
        assert probe.chain_calls == 0 and probe.served == [] and docker_calls == []

    def test_no_chain_publishes_nothing(self, runner, docker_calls, probe):
        args = [*UP, '--no-update', '--allow-dev-keys', '--no-chain', '--image', 'entrius/gt-agent:dev', '--json']
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        checks = _json_checks(result)
        assert checks['Public IP']['status'] == 'skip' and checks['Endpoint published']['status'] == 'skip'
        assert probe.chain_calls == 0 and probe.served == []

    def test_an_unanswering_ssh_port_warns_and_can_be_skipped(self, runner, docker_calls, probe):
        probe.answers = False
        result = runner.invoke(cli, [*UP, '--json'])
        assert result.exit_code == 0, result.output
        row = _json_checks(result)['SSH port reachable']
        assert row['status'] == 'warn' and 'nc -vz 44.10.0.1 2200' in row['detail'] and probe.served
        skipped = runner.invoke(cli, [*UP, '--skip-reachability', '--json'])
        assert _json_checks(skipped)['SSH port reachable']['status'] == 'skip'

    def test_real_probe_reachable_listens_on_a_free_port(self):
        import socket

        real = prereqs.HostProbe()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        assert real.reachable('127.0.0.1', port) is True  # nothing listened: the probe's own listener answered


BOX = ['--ip', '88.22.127.152', '--ssh-port', '20234']


class TestPublishOnly:
    """The wallet here, the box elsewhere (a rented pod, or a miner who keeps keys off the GPU box)."""

    def test_serves_from_here_with_no_host_checks_and_no_docker(self, runner, docker_calls, probe):
        probe.smi = subprocess.CompletedProcess([], 127, '', 'nvidia-smi: command not found')  # no GPU on this machine
        probe.docker_info = subprocess.CompletedProcess([], 1, '', 'no daemon')
        with patch('gittensor.cli.up_commands.up._load_channel', side_effect=AssertionError('must not be called')):
            result = runner.invoke(cli, [*UP, '--publish-only', *BOX, '--json'])
        assert result.exit_code == 0, result.output
        assert probe.served == [('alice', 'default', 74, '88.22.127.152', 20234)] and docker_calls == []
        payload = json.loads(result.stdout)
        assert payload['publish_only'] and payload['commands'] == [] and payload['agent_command'] == ''
        assert payload['endpoint']['published'] == 'served' and payload['endpoint']['port'] == 20234
        assert list(_json_checks(result)) == [
            'Public IP',
            'SSH port reachable',
            'Wallet hotkey',
            'Registered on netuid 74',
            'Endpoint published',
        ]

    def test_an_unchanged_endpoint_is_not_served_again(self, runner, docker_calls, probe):
        probe.on_chain = ('88.22.127.152', 20234, True)
        result = runner.invoke(cli, [*UP, '--publish-only', *BOX, '--json'])
        assert result.exit_code == 0 and probe.served == []
        assert json.loads(result.stdout)['endpoint']['published'] == 'unchanged'
        probe.on_chain = ('88.22.127.152', 20234, False)  # a plain axon at the same address: re-served with the marker
        assert runner.invoke(cli, [*UP, '--publish-only', *BOX]).exit_code == 0 and len(probe.served) == 1

    def test_dry_run_prints_and_touches_no_chain(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--publish-only', *BOX, '--dry-run'])
        assert result.exit_code == 0, result.output
        assert 'would publish 88.22.127.152:20234 on netuid 74' in result.output and 'Would run' not in result.output
        assert probe.chain_calls == 0 and probe.served == [] and docker_calls == []

    def test_needs_ip_and_refuses_box_flags(self, runner, docker_calls, probe):
        result = runner.invoke(cli, [*UP, '--publish-only'])
        assert result.exit_code == 2 and 'needs --ip' in result.output
        for flag in (['--no-chain', '--no-update'], ['--no-update'], ['--no-update', '--allow-dev-keys']):
            result = runner.invoke(cli, [*UP, '--publish-only', *BOX, *flag])
            assert result.exit_code == 2 and '--publish-only' in result.output, flag
        assert probe.chain_calls == 0 and docker_calls == []

    def test_unregistered_or_failed_serve_exits_1(self, runner, docker_calls, probe):
        probe.registered = False
        result = runner.invoke(cli, [*UP, '--publish-only', *BOX])
        assert result.exit_code == 1 and probe.served == [] and 'not registered' in result.output
        probe.registered, probe.serve_error = True, 'ServingRateLimitExceeded'
        result = runner.invoke(cli, [*UP, '--publish-only', *BOX, '--json'])
        assert (
            result.exit_code == 1 and 'ServingRateLimitExceeded' in _json_checks(result)['Endpoint published']['detail']
        )
        assert docker_calls == []
