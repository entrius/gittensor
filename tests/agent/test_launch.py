# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The docker run lines (and their parity with docker/agent/runner.sh), the docker assets, and the release channel."""

import json
import subprocess
from pathlib import Path

import pytest

from gittensor.agent import channel, config
from gittensor.agent.launch import (
    AGENT_PRIVILEGE_FLAGS,
    agent_run_command,
    down_commands,
    render,
    runner_run_command,
)

REPO = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO / 'docker' / 'agent'
RUNNER_SH = AGENT_DIR / 'runner.sh'
DIGEST = 'sha256:' + 'a' * 64
AGENT_REF = f'entrius/gt-agent@{DIGEST}'
RUNNER_REF = 'entrius/gt-agent-runner@sha256:' + 'b' * 64


class TestRunLines:
    def test_agent_line_is_the_documented_privileged_footprint(self):
        cmd = agent_run_command(image=AGENT_REF, ssh_port=2200, miner_hotkey='5Hot', image_digest=DIGEST)
        line = render(cmd)
        assert line.startswith('docker run -d --name gt-agent --restart unless-stopped')
        for flag in ('--privileged', '--pid host', '--gpus all', '-v /var/run/docker.sock:/var/run/docker.sock'):
            assert flag in line
        assert '-v gt-agent-ssh:/var/lib/gt-agent' in line
        assert '-p 2200:2200' in line and '8200' not in line  # one port: sshd; no agent HTTP port
        assert '-e GT_AGENT_SSH_PORT=2200 -e GT_AGENT_MINER_HOTKEY=5Hot' in line
        assert f'-e GT_AGENT_IMAGE_DIGEST={DIGEST}' in line
        assert 'GT_AGENT_ALLOW_DEV_KEYS' not in line
        assert cmd[-1] == AGENT_REF

    def test_dev_keys_override_is_explicit(self):
        line = render(agent_run_command(image='entrius/gt-agent:dev', ssh_port=2200, allow_dev_keys=True))
        assert '-e GT_AGENT_ALLOW_DEV_KEYS=1' in line

    def test_runner_line_needs_only_the_socket_and_follows_the_channel(self):
        cmd = runner_run_command(runner_image=RUNNER_REF, ssh_port=2200, miner_hotkey='5Hot')
        line = render(cmd)
        assert line.startswith('docker run -d --name gt-agent-runner --restart unless-stopped')
        assert '--privileged' not in line and '--gpus' not in line
        assert '-v /var/run/docker.sock:/var/run/docker.sock' in line
        for env in (
            f'GT_AGENT_CHANNEL_URL={config.AGENT_CHANNEL_URL}',
            'GT_AGENT_CONTAINER_NAME=gt-agent',
            'GT_AGENT_SSH_PORT=2200',
            'GT_AGENT_MINER_HOTKEY=5Hot',
        ):
            assert f'-e {env}' in line
        assert 'GT_AGENT_IMAGE=' not in line  # the runner learns the agent image from the channel, not from us
        assert f'-e GT_AGENT_UPDATE_INTERVAL_S={config.UPDATE_INTERVAL_S}' in line
        assert cmd[-1] == RUNNER_REF

    def test_down_removes_runner_before_agent(self):
        assert down_commands() == [['docker', 'rm', '-f', 'gt-agent-runner'], ['docker', 'rm', '-f', 'gt-agent']]

    def test_runner_script_issues_the_same_agent_flags(self):
        """runner.sh reproduces agent_run_command in shell; keep the two from drifting apart."""
        script = RUNNER_SH.read_text()
        run_block = script[script.index('docker run -d') : script.index('"$image"', script.index('docker run -d'))]
        for flag in AGENT_PRIVILEGE_FLAGS:
            assert flag in run_block
        for needle in (
            '--restart unless-stopped',
            '-v /var/run/docker.sock:/var/run/docker.sock',
            '"$VOLUME:/var/lib/gt-agent"',
            '-p "$SSH_PORT:$SSH_PORT"',
            'GT_AGENT_SSH_PORT=$SSH_PORT',
            'GT_AGENT_MINER_HOTKEY=$MINER_HOTKEY',
            'GT_AGENT_IMAGE=$image',
            'GT_AGENT_IMAGE_DIGEST=$digest',
            'NVIDIA_DRIVER_CAPABILITIES=all',
        ):
            assert needle in run_block, needle
        assert '8200' not in run_block and 'HTTP_PORT' not in script
        # every env var the runner reads is one the CLI sets on it
        for env in (
            config.ENV_CHANNEL_URL,
            config.ENV_CONTAINER_NAME,
            config.ENV_SSH_PORT,
            config.ENV_MINER_HOTKEY,
            config.ENV_UPDATE_INTERVAL,
        ):
            assert f'${{{env}' in script, env

    def test_runner_verifies_the_channel_and_never_prunes_host_wide(self):
        script = RUNNER_SH.read_text()
        assert 'ssh-keygen -Y verify' in script and '-n "$SIGN_NAMESPACE"' in script
        assert 'docker image prune' not in script
        assert 'reference=$AGENT_REPO' in script
        assert f'"{config.RELEASE_SIGN_NAMESPACE}"' in script or f'{config.RELEASE_SIGN_NAMESPACE}}}' in script

    def test_shell_scripts_parse(self):
        for script in ('runner.sh', 'entrypoint.sh', 'keys/make-dev-keys.sh', 'channel/sign.sh'):
            proc = subprocess.run(['bash', '-n', str(AGENT_DIR / script)], capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr

    def test_agent_image_is_sshd_only_by_certificate(self):
        sshd = (AGENT_DIR / 'sshd.conf').read_text()
        assert f'TrustedUserCAKeys {config.CA_PUBKEY_PATH}' in sshd
        assert 'AuthorizedKeysFile none' in sshd and 'PasswordAuthentication no' in sshd
        dockerfile = (AGENT_DIR / 'Dockerfile').read_text()
        assert 'ARG GT_CA_PUB' in dockerfile and dockerfile.count('FROM ') == 1 and 'FROM debian' in dockerfile
        assert 'pip install' not in dockerfile and 'COPY gittensor' not in dockerfile  # no Python in the agent
        assert 'EXPOSE 2200\n' in dockerfile and '8200' not in dockerfile
        entrypoint = (AGENT_DIR / 'entrypoint.sh').read_text()
        assert config.DEV_KEY_MARKER in entrypoint and config.ENV_ALLOW_DEV_KEYS in entrypoint

    def test_no_key_material_is_committed(self):
        ignored = (AGENT_DIR / 'keys' / '.gitignore').read_text().splitlines()
        assert (
            ignored[:2]
            == [
                '# Key material never goes in the repo: not the dev pairs make-dev-keys.sh writes here, not the real public halves.',
                '*',
            ]
            or '*' in ignored
        )
        # make-dev-keys.sh writes the dev pairs here on purpose (gitignored); what must never happen is a commit
        tracked = subprocess.run(
            ['git', '-C', str(REPO), 'ls-files', 'docker/agent/keys'], capture_output=True, text=True
        ).stdout.split()
        assert not [p for p in tracked if Path(p).name.startswith('gt_')]
        assert (
            config.RELEASE_PUBKEY_OPENSSH.startswith('ssh-ed25519 ')
            and config.DEV_KEY_MARKER not in config.RELEASE_PUBKEY_OPENSSH
        )


# --- the release channel --------------------------------------------------------------------------------------------


def have_ssh_keygen() -> bool:
    try:
        return subprocess.run(['ssh-keygen', '-?'], capture_output=True).returncode in (0, 1, 255)
    except OSError:
        return False


@pytest.fixture(scope='module')
def release_key(tmp_path_factory):
    if not have_ssh_keygen():
        pytest.skip('ssh-keygen not installed')
    d = tmp_path_factory.mktemp('release')
    subprocess.run(
        ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'test-release', '-f', str(d / 'k')], check=True
    )
    return d / 'k', (d / 'k.pub').read_text()


def sign(key: Path, payload: bytes, namespace: str = config.RELEASE_SIGN_NAMESPACE) -> bytes:
    proc = subprocess.run(
        ['ssh-keygen', '-Y', 'sign', '-f', str(key), '-n', namespace, '-q'], input=payload, capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


CHANNEL = {'agent': AGENT_REF, 'runner': RUNNER_REF, 'version': '5.1.0', 'issued_at': 1_789_000_000}


class TestChannel:
    def test_parse_happy_path_and_digest(self):
        c = channel.parse(json.dumps(CHANNEL).encode())
        assert c.agent == AGENT_REF and c.runner == RUNNER_REF and c.version == '5.1.0'
        assert c.agent_digest == DIGEST

    @pytest.mark.parametrize(
        'bad',
        [
            b'not json',
            b'[]',
            b'{"agent": "entrius/gt-agent:stable", "runner": "%s"}' % RUNNER_REF.encode(),  # a tag, not a digest
            b'{"agent": "evil/gt-agent@%s", "runner": "%s"}' % (DIGEST.encode(), RUNNER_REF.encode()),  # wrong repo
            b'{"agent": "%s"}' % AGENT_REF.encode(),  # missing runner
            b'{"agent": "%s", "runner": "%s", "issued_at": "soon"}' % (AGENT_REF.encode(), RUNNER_REF.encode()),
        ],
    )
    def test_parse_rejects(self, bad):
        with pytest.raises(channel.ChannelError):
            channel.parse(bad)

    def test_allowed_signers_line_drops_the_comment(self):
        line = channel.allowed_signers_line('ssh-ed25519 AAAAC3 some comment here')
        assert line == 'gittensor-release namespaces="gt-agent-channel" ssh-ed25519 AAAAC3\n'

    def test_verify_requires_a_compiled_in_key(self):
        with pytest.raises(channel.ChannelError, match='no release public key'):
            channel.verify(b'{}', b'sig', pubkey='')

    def test_verify_with_a_real_signature(self, release_key):
        key, pub = release_key
        payload = json.dumps(CHANNEL).encode()
        channel.verify(payload, sign(key, payload), pubkey=pub)  # good
        with pytest.raises(channel.ChannelError, match='does not verify'):
            channel.verify(payload + b'\n', sign(key, payload), pubkey=pub)  # tampered payload
        with pytest.raises(channel.ChannelError, match='does not verify'):
            channel.verify(payload, sign(key, payload, namespace='file'), pubkey=pub)  # wrong namespace
        other = subprocess.run(
            ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key.parent / 'other')], capture_output=True
        )
        assert other.returncode == 0
        with pytest.raises(channel.ChannelError, match='does not verify'):
            channel.verify(payload, sign(key.parent / 'other', payload), pubkey=pub)  # wrong key

    def test_load_fetches_both_files_and_verifies(self, release_key):
        key, pub = release_key
        payload = json.dumps(CHANNEL).encode()
        files = {'https://x/stable.json': payload, 'https://x/stable.json.sig': sign(key, payload)}

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(url, timeout):
            if url not in files:
                raise OSError(f'404 {url}')
            return Resp(files[url])

        c = channel.load('https://x/stable.json', pubkey=pub, opener=opener)
        assert c.agent == AGENT_REF
        with pytest.raises(channel.ChannelError, match='fetch'):
            channel.load('https://x/missing.json', pubkey=pub, opener=opener)
