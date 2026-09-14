# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The docker run lines (and their parity with docker/agent/runner.sh), settings from env, and GPU parsing."""

import subprocess
from pathlib import Path

from gittensor.agent import config
from gittensor.agent.config import AgentSettings
from gittensor.agent.gpu import (
    GpuInventory,
    gpu_inventory,
    inventory_via_nvidia_smi,
    inventory_via_nvml,
    parse_nvidia_smi_csv,
)
from gittensor.agent.launch import (
    AGENT_PRIVILEGE_FLAGS,
    agent_run_command,
    down_commands,
    render,
    runner_run_command,
)

REPO = Path(__file__).resolve().parents[2]
RUNNER_SH = REPO / 'docker' / 'agent' / 'runner.sh'


class TestRunLines:
    def test_agent_line_is_the_documented_privileged_footprint(self):
        cmd = agent_run_command(ssh_port=2200, http_port=8200, miner_hotkey='5Hot', image_digest='sha256:1')
        line = render(cmd)
        assert line.startswith('docker run -d --name gt-agent --restart unless-stopped')
        for flag in ('--privileged', '--pid host', '--gpus all', '-v /var/run/docker.sock:/var/run/docker.sock'):
            assert flag in line
        assert '-v gt-agent-ssh:/var/lib/gt-agent' in line
        assert '-p 2200:2200 -p 8200:8200' in line
        assert '-e GT_AGENT_SSH_PORT=2200 -e GT_AGENT_HTTP_PORT=8200 -e GT_AGENT_MINER_HOTKEY=5Hot' in line
        assert '-e GT_AGENT_IMAGE_DIGEST=sha256:1' in line
        assert cmd[-1] == config.AGENT_IMAGE

    def test_runner_line_needs_only_the_socket(self):
        cmd = runner_run_command(ssh_port=2200, http_port=8200, miner_hotkey='5Hot')
        line = render(cmd)
        assert line.startswith('docker run -d --name gt-agent-runner --restart unless-stopped')
        assert '--privileged' not in line and '--gpus' not in line
        assert '-v /var/run/docker.sock:/var/run/docker.sock' in line
        for env in (
            'GT_AGENT_IMAGE=entrius/gt-agent:stable',
            'GT_AGENT_CONTAINER_NAME=gt-agent',
            'GT_AGENT_SSH_PORT=2200',
        ):
            assert f'-e {env}' in line
        assert f'-e GT_AGENT_UPDATE_INTERVAL_S={config.UPDATE_INTERVAL_S}' in line
        assert cmd[-1] == config.RUNNER_IMAGE

    def test_down_removes_runner_before_agent(self):
        assert down_commands() == [['docker', 'rm', '-f', 'gt-agent-runner'], ['docker', 'rm', '-f', 'gt-agent']]

    def test_runner_script_issues_the_same_agent_flags(self):
        """runner.sh reproduces agent_run_command in shell; keep the two from drifting apart."""
        script = RUNNER_SH.read_text()
        run_block = script[script.index('docker run -d') : script.index('"$IMAGE"', script.index('docker run -d'))]
        for flag in AGENT_PRIVILEGE_FLAGS:
            assert flag in run_block
        for needle in (
            '--restart unless-stopped',
            '-v /var/run/docker.sock:/var/run/docker.sock',
            '"$VOLUME:/var/lib/gt-agent"',
            '-p "$SSH_PORT:$SSH_PORT"',
            '-p "$HTTP_PORT:$HTTP_PORT"',
            'GT_AGENT_SSH_PORT=$SSH_PORT',
            'GT_AGENT_HTTP_PORT=$HTTP_PORT',
            'GT_AGENT_MINER_HOTKEY=$MINER_HOTKEY',
            'GT_AGENT_IMAGE=$IMAGE',
            'GT_AGENT_IMAGE_DIGEST=$digest',
            'NVIDIA_DRIVER_CAPABILITIES=all',
        ):
            assert needle in run_block, needle
        # every env var the runner reads is one the CLI sets on it
        for env in (
            config.ENV_IMAGE,
            config.ENV_CONTAINER_NAME,
            config.ENV_SSH_PORT,
            config.ENV_HTTP_PORT,
            config.ENV_MINER_HOTKEY,
            config.ENV_UPDATE_INTERVAL,
        ):
            assert f'${{{env}' in script, env

    def test_shell_scripts_parse(self):
        for script in ('runner.sh', 'entrypoint.sh'):
            proc = subprocess.run(
                ['bash', '-n', str(REPO / 'docker' / 'agent' / script)], capture_output=True, text=True
            )
            assert proc.returncode == 0, proc.stderr


class TestSettings:
    def test_defaults(self):
        s = AgentSettings.from_env({})
        assert (s.http_port, s.ssh_port, s.miner_hotkey, s.image) == (8200, 2200, '', config.AGENT_IMAGE)
        assert s.authorized_keys_path == '/root/.ssh/authorized_keys'

    def test_env_overrides(self):
        s = AgentSettings.from_env(
            {
                'GT_AGENT_HTTP_PORT': '9000',
                'GT_AGENT_SSH_PORT': '2201',
                'GT_AGENT_MINER_HOTKEY': '5Hot',
                'GT_AGENT_IMAGE_DIGEST': 'sha256:abc',
                'GT_AGENT_AUTHORIZED_KEYS': '/tmp/ak',
            }
        )
        assert (s.http_port, s.ssh_port, s.miner_hotkey, s.image_digest, s.authorized_keys_path) == (
            9000,
            2201,
            '5Hot',
            'sha256:abc',
            '/tmp/ak',
        )


class TestGpu:
    def test_parse_nvidia_smi_csv(self):
        text = (
            'GPU-1111, NVIDIA GeForce RTX 5090, 580.65.06, 32607\nGPU-2222, NVIDIA GeForce RTX 5090, 580.65.06, 32607\n'
        )
        driver, gpus = parse_nvidia_smi_csv(text)
        assert driver == '580.65.06'
        assert [g.uuid for g in gpus] == ['GPU-1111', 'GPU-2222']
        assert gpus[0].memory_total_mib == 32607

    def test_nvidia_smi_missing_is_an_error_not_an_exception(self):
        def run(*a, **k):
            raise FileNotFoundError('nvidia-smi')

        inv = inventory_via_nvidia_smi(run=run)
        assert inv.gpus == [] and 'nvidia-smi' in inv.error and inv.source == 'nvidia-smi'

    def test_nvidia_smi_nonzero_exit(self):
        def run(*a, **k):
            return subprocess.CompletedProcess(a, 9, '', 'NVIDIA-SMI has failed')

        inv = inventory_via_nvidia_smi(run=run)
        assert inv.error == 'NVIDIA-SMI has failed'

    def test_nvml_library_missing_is_an_error(self):
        inv = inventory_via_nvml('libnvidia-ml-does-not-exist.so.1')
        assert inv.source == 'nvml' and inv.error and inv.gpus == []

    def test_inventory_falls_back_and_reports_both_errors(self, monkeypatch):
        import gittensor.agent.gpu as gpu

        monkeypatch.setattr(gpu, 'inventory_via_nvml', lambda: GpuInventory(None, source='nvml', error='no lib'))
        monkeypatch.setattr(
            gpu, 'inventory_via_nvidia_smi', lambda: GpuInventory(None, source='nvidia-smi', error='no smi')
        )
        inv = gpu_inventory()
        assert inv.source == 'none' and inv.error == 'nvml: no lib; nvidia-smi: no smi'

    def test_inventory_prefers_nvml(self, monkeypatch):
        import gittensor.agent.gpu as gpu

        monkeypatch.setattr(gpu, 'inventory_via_nvml', lambda: GpuInventory('580', [], source='nvml'))
        assert gpu_inventory().source == 'nvml'
