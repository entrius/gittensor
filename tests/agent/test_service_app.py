# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The service behind the routes, and the real HTTP server on an ephemeral port."""

import http.client
import json
import threading

import pytest

from gittensor.agent.app import make_server
from gittensor.agent.auth import ACTION_REMOVE
from gittensor.agent.config import AgentSettings
from gittensor.agent.gpu import GpuInfo, GpuInventory
from gittensor.agent.service import AgentService
from tests.agent.conftest import TEST_PUBKEY, TEST_PUBKEY_BODY


def _fake_inventory():
    return GpuInventory('580.65.06', [GpuInfo('GPU-aaaa', 'NVIDIA GeForce RTX 5090', 32607)], source='nvml')


@pytest.fixture
def settings(tmp_path):
    return AgentSettings(
        http_port=0,
        ssh_port=2222,
        miner_hotkey='5MinerHotkey',
        image='entrius/gt-agent:test',
        image_digest='sha256:feed',
        authorized_keys_path=str(tmp_path / 'authorized_keys'),
    )


@pytest.fixture
def service(settings, verifier, clock):
    return AgentService(settings, verifier=verifier, inventory=_fake_inventory, clock=clock)


class TestService:
    def test_info_reports_static_and_gpu_fields(self, service, controller_keypair):
        status, body = service.info()
        assert status == 200
        assert body['sshd_port'] == 2222
        assert body['ssh_user'] == 'root'
        assert body['image_digest'] == 'sha256:feed'
        assert body['controller_hotkey'] == controller_keypair.ss58_address
        assert body['miner_hotkey'] == '5MinerHotkey'
        assert body['driver_version'] == '580.65.06'
        assert body['gpus'] == [{'uuid': 'GPU-aaaa', 'name': 'NVIDIA GeForce RTX 5090', 'memory_total_mib': 32607}]
        assert body['installed_keys'] == 0

    def test_install_then_remove_round_trip(self, service, signed, settings):
        status, body = service.install_ssh_key(signed())
        assert (status, body['installed'], body['key']) == (200, True, TEST_PUBKEY_BODY)
        assert body['ssh_port'] == 2222
        assert service.info()[1]['installed_keys'] == 1
        # second install with a fresh nonce: accepted, nothing added
        status, body = service.install_ssh_key(signed())
        assert (status, body['installed']) == (200, False)
        status, body = service.remove_ssh_key(signed(action=ACTION_REMOVE))
        assert (status, body['removed']) == (200, True)
        assert open(settings.authorized_keys_path).read() == ''

    def test_bad_signature_is_401_and_writes_nothing(self, service, signed, attacker_keypair, settings):
        status, body = service.install_ssh_key(signed(keypair=attacker_keypair))
        assert status == 401 and 'error' in body
        assert not __import__('os').path.exists(settings.authorized_keys_path)

    def test_replay_is_401(self, service, signed):
        body = signed()
        assert service.install_ssh_key(body)[0] == 200
        assert service.install_ssh_key(body)[0] == 401

    def test_bad_shape_is_400(self, service):
        status, body = service.install_ssh_key({'pubkey': TEST_PUBKEY})
        assert status == 400 and 'missing' in body['error']


@pytest.fixture
def live(service):
    server = make_server(service, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()


def _call(addr, method, path, body=None, raw=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {'Content-Type': 'application/json'} if payload is not None else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b'null')
    conn.close()
    return resp.status, data


class TestHTTP:
    def test_get_info(self, live):
        status, body = _call(live, 'GET', '/info')
        assert status == 200 and body['sshd_port'] == 2222

    def test_post_and_delete_key(self, live, signed, settings):
        status, body = _call(live, 'POST', '/install_ssh_key', signed())
        assert status == 200 and body['installed'] is True
        assert TEST_PUBKEY_BODY in open(settings.authorized_keys_path).read()
        status, body = _call(live, 'DELETE', '/install_ssh_key', signed(action=ACTION_REMOVE))
        assert status == 200 and body['removed'] is True

    def test_forged_post_is_401(self, live, signed, attacker_keypair):
        status, _ = _call(live, 'POST', '/install_ssh_key', signed(keypair=attacker_keypair))
        assert status == 401

    @pytest.mark.parametrize(
        ('method', 'path'),
        [
            ('GET', '/'),
            ('GET', '/install_ssh_key'),
            ('POST', '/info'),
            ('POST', '/upload_ssh_key'),
            ('DELETE', '/info'),
        ],
    )
    def test_no_other_routes(self, live, method, path):
        status, body = _call(live, method, path, body={} if method != 'GET' else None)
        assert status == 404 and body == {'error': 'not found'}

    def test_non_json_body_is_400(self, live):
        status, body = _call(live, 'POST', '/install_ssh_key', raw=b'not json')
        assert status == 400 and 'JSON' in body['error']

    def test_oversized_body_is_413(self, live):
        status, _ = _call(live, 'POST', '/install_ssh_key', raw=b'{"pubkey": "' + b'A' * 20000 + b'"}')
        assert status == 413
