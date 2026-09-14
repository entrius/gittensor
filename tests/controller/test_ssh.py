# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Per-visit certificates (real ssh-keygen) and the SshRunner's ssh invocation (fake subprocess)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.ssh import (
    CertificateAuthority,
    SshRunner,
    SshTransportError,
    cert_details,
    known_hosts_line,
    scan_host_key,
)
from gittensor.controller.ssh.certs import CertificateError

pytestmark = pytest.mark.skipif(shutil.which('ssh-keygen') is None, reason='ssh-keygen not installed')


@pytest.fixture(scope='module')
def ca_key(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp('ca')
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'test-ca', '-f', str(d / 'ca')], check=True)
    return d / 'ca'


class FakeClock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestMint:
    def test_certificate_is_root_for_five_minutes_and_discardable(self, ca_key):
        clock = FakeClock()
        ca = CertificateAuthority(ca_key, clock=clock)
        cred = ca.mint('ctl-heartbeat-box1')
        try:
            assert cred.key_path.is_file() and cred.cert_path.is_file()
            assert cred.principal == 'root' and cred.key_id == 'ctl-heartbeat-box1'
            assert cred.not_after - cred.not_before == 300 + 60
            details = cert_details(cred.cert_path)
            assert details['Key ID'] == '"ctl-heartbeat-box1"'
            assert details['Principals'] == 'root'
            assert 'user certificate' in details['Type']
            assert 'ED25519-CERT' in details['Public key']
            assert not cred.expired(clock.now) and not cred.expired(clock.now + 200)
            assert cred.expired(clock.now + 300 - 10)  # inside the 30 s margin
            assert cred.expired(clock.now + 301)
        finally:
            cred.discard()
        assert not cred.workdir.exists()

    def test_source_address_and_other_principal(self, ca_key):
        cred = CertificateAuthority(ca_key).mint(
            'human-kimbo', principal='root', validity_s=3600, source_address='10.0.0.0/8'
        )
        try:
            details = cert_details(cred.cert_path)
            assert 'source-address 10.0.0.0/8' in details['Critical Options']
        finally:
            cred.discard()

    def test_bad_inputs_leave_nothing_behind(self, ca_key, tmp_path):
        ca = CertificateAuthority(ca_key)
        with pytest.raises(CertificateError):
            ca.mint('has space')
        with pytest.raises(CertificateError):
            ca.mint('ok', validity_s=0)
        with pytest.raises(CertificateError, match='failed'):
            CertificateAuthority(tmp_path / 'no-such-ca').mint('ok')
        assert not [
            p
            for p in Path('/tmp').glob('gt-visit-*')
            if (p / 'id_ed25519.pub').exists() and not (p / 'id_ed25519-cert.pub').exists()
        ]

    def test_missing_ssh_keygen(self, ca_key):
        with pytest.raises(CertificateError, match='no-such-ssh-keygen'):
            CertificateAuthority(ca_key, ssh_keygen='no-such-ssh-keygen').mint('ok')


class TestSshRunner:
    def _runner(self, ca_key, tmp_path, calls, returncode=0, stdout=b'ok\n', stderr=b'', clock=None):
        def run(argv, input=None, capture_output=True, timeout=None):
            calls.append((argv, input, timeout))
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        known = tmp_path / 'known_hosts'
        known.write_text(
            known_hosts_line(
                '203.0.113.7',
                2200,
                'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA pinned',
            )
        )
        return SshRunner(
            '203.0.113.7',
            2200,
            CertificateAuthority(ca_key, clock=clock or FakeClock()),
            known,
            'ctl-check-box1',
            run=run,
            clock=clock or FakeClock(),
        )

    def test_argv_pins_host_key_and_uses_the_certificate(self, ca_key, tmp_path):
        calls = []
        with self._runner(ca_key, tmp_path, calls) as r:
            result = r.run('nvidia-smi -L', timeout=30)
            assert result == CommandResult(0, 'ok\n', '')
            ((argv, stdin, timeout),) = calls
            cred = r.credential()
            assert argv[0] == 'ssh' and argv[-1] == 'nvidia-smi -L' and argv[-2] == '--'
            assert argv[1:3] == ['-i', str(cred.key_path)]
            assert f'CertificateFile={cred.cert_path}' in argv
            assert 'StrictHostKeyChecking=yes' in argv and f'UserKnownHostsFile={tmp_path / "known_hosts"}' in argv
            assert 'BatchMode=yes' in argv and 'IdentitiesOnly=yes' in argv
            assert argv[argv.index('-p') + 1] == '2200' and 'root@203.0.113.7' in argv
            assert timeout == 30 and stdin is None
            assert r.minted == 1
            r.run('true')
            assert r.minted == 1  # same visit, same certificate
            workdir = cred.workdir
        assert not workdir.exists()  # discarded on exit

    def test_stdin_is_forwarded(self, ca_key, tmp_path):
        calls = []
        with self._runner(ca_key, tmp_path, calls) as r:
            r.run('docker cp - c1:/opt/gt-proof/bin', stdin=b'TARBYTES')
        assert calls[0][1] == b'TARBYTES'

    def test_certificate_is_reminted_near_expiry(self, ca_key, tmp_path):
        clock = FakeClock()
        calls = []
        with self._runner(ca_key, tmp_path, calls, clock=clock) as r:
            r.run('true')
            first = r.credential().cert_path
            clock.now += 280  # 20 s before not_after: inside the margin
            r.run('true')
            assert r.minted == 2 and r.credential().cert_path != first and not first.exists()

    def test_transport_failure_raises_and_command_failure_returns(self, ca_key, tmp_path):
        calls = []
        with self._runner(ca_key, tmp_path, calls, returncode=255, stderr=b'Host key verification failed.') as r:
            with pytest.raises(SshTransportError, match='Host key verification failed'):
                r.run('true')
        with self._runner(ca_key, tmp_path, calls, returncode=3, stdout=b'', stderr=b'boom') as r:
            assert r.run('exit 3') == CommandResult(3, '', 'boom')

        def timeout_run(argv, input=None, capture_output=True, timeout=None):
            raise subprocess.TimeoutExpired(argv, timeout)

        known = tmp_path / 'kh'
        known.write_text('')
        r = SshRunner('h', 2200, CertificateAuthority(ca_key), known, 'k', run=timeout_run)
        with pytest.raises(SshTransportError, match='timed out'):
            r.run('sleep 99', timeout=1)
        r.close()

    def test_known_hosts_line_and_scan(self):
        assert known_hosts_line('h', 2200, 'ssh-ed25519 AAAA comment') == '[h]:2200 ssh-ed25519 AAAA\n'

        def run(argv, capture_output, text, timeout):
            return subprocess.CompletedProcess(argv, 0, '# h:2200 SSH-2.0\n[h]:2200 ssh-ed25519 AAAAKEY\n', '')

        assert scan_host_key('h', 2200, run=run) == 'ssh-ed25519 AAAAKEY'
        with pytest.raises(SshTransportError):
            scan_host_key('h', 2200, run=lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'refused'))
