# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt rent against an in-memory product API: the order it sends, the wait for active, the ssh line, the retry
on a failed start, the up-front refusal when nothing is free, names, rm's cost line, and the API's own errors."""

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.rent_commands import api as rapi
from gittensor.cli.rent_commands import rent as rcmd

KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests0000000000000000000000000 alex'
OFFERS = {
    'offers': [
        {
            'gpu_type': 'RTX5090',
            'usd_per_card_hr': 0.65,
            'boxes': [
                {'gpu_count': 1, 'usd_per_hr': 0.65, 'available': 1},
                {'gpu_count': 2, 'usd_per_hr': 1.3, 'available': 0},
            ],
        },
        {'gpu_type': 'H100', 'usd_per_card_hr': 1.49, 'boxes': [{'gpu_count': 8, 'usd_per_hr': 11.92, 'available': 0}]},
    ],
    'fleet': 'ok',
    'images': [{'name': 'PyTorch', 'image': 'daturaai/pytorch:2.6.0-py3.12-cuda12.6.3-devel-ubuntu24.04'}],
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeProduct:
    """gittensor-app's customer routes in memory. Each GET of a rental advances its scripted states."""

    def __init__(self, script=('requested', 'starting', 'active'), fail_order: dict | None = None):
        self.script, self.fail_order, self.fail_reason = list(script), fail_order, 'start_failed'
        self.rentals: dict[str, dict] = {}
        self.reads: dict[str, int] = {}
        self.orders: list[dict] = []
        self.calls: list[str] = []
        self.balance = {'balance_cents': 984, 'burn_cents_per_hr': 0, 'entries': []}
        self.n = 0

    def __call__(self, req, timeout=None):
        m, path = req.get_method(), req.full_url.split('http://api.test', 1)[1]
        self.calls.append(f'{m} {path}')
        assert req.get_header('Authorization') == 'Bearer gt_testkey'
        if (m, path) == ('GET', '/rentals/offers'):
            return self._ok(OFFERS)
        if (m, path) == ('GET', '/balance'):
            return self._ok(self.balance)
        if (m, path) == ('GET', '/rentals'):
            return self._ok({'rentals': [self._view(i) for i in self.rentals]})
        if (m, path) == ('POST', '/rentals'):
            if self.fail_order:
                return self._err(self.fail_order['status'], self.fail_order['message'], self.fail_order['type'])
            body = json.loads(req.data)
            self.orders.append(body)
            self.n += 1
            rid = f'rnt_{self.n:03d}'
            self.rentals[rid] = {**body, 'id': rid, 'fail_reason': self.fail_reason}
            self.reads[rid] = 0
            return self._ok(self._view(rid))
        if m == 'GET' and path.startswith('/rentals/'):
            rid = path.rsplit('/', 1)[1]
            if rid not in self.rentals:
                return self._err(404, f'no rental {rid}', 'not_found')
            self.reads[rid] += 1
            return self._ok(self._view(rid))
        if m == 'DELETE':
            rid = path.rsplit('/', 1)[1]
            self.rentals[rid]['stopping'] = True
            self.reads[rid] = 0
            return self._ok(self._view(rid))
        if m == 'POST' and path.endswith('/extend'):
            rid = path.split('/')[2]
            self.rentals[rid]['ends_at'] = 5000 + int(json.loads(req.data)['hours'] * 3600)
            return self._ok(self._view(rid))
        raise AssertionError(f'unexpected {m} {path}')

    def _view(self, rid: str) -> dict:
        r = self.rentals[rid]
        i = min(self.reads.get(rid, 0), len(self.script) - 1)
        if r.get('stopping'):
            state = ['ending', 'ended'][min(self.reads[rid], 1)]
        else:
            state = self.script[i]
        out = {
            'id': rid,
            'state': state,
            'gpu_type': r['gpu_type'],
            'gpu_count': r['gpu_count'],
            'image': r['image'],
            'host': None,
            'port_map': None,
            'ssh': None,
            'ends_at': r.get('ends_at', 5000),
            'started_at': None,
            'ended_at': None,
            'reason': None,
            'billed_cents': 0,
            'usd_cents_per_hr': 65,
        }
        if state in ('active', 'ending', 'ended'):
            out.update(
                host='203.0.113.7',
                port_map={'22': 40047, '8888': 40045},
                started_at=1005,
                ssh='ssh root@203.0.113.7 -p 40047',
            )
        if state == 'ended':
            out.update(ended_at=1077, billed_cents=16, reason='customer_stop')
        if state == 'failed':
            out.update(reason=r.get('fail_reason', 'start_failed'))
        return out

    @staticmethod
    def _ok(obj):
        return _Resp(json.dumps(obj).encode())

    @staticmethod
    def _err(status, message, kind):
        body = json.dumps({'error': {'message': message, 'type': kind}}).encode()
        raise urllib.error.HTTPError('http://api.test', status, message, Message(), io.BytesIO(body))


@pytest.fixture
def product(monkeypatch, tmp_path):
    app = FakeProduct()
    monkeypatch.setattr(rapi.urllib.request, 'urlopen', app)
    monkeypatch.setattr(rapi, 'RENT_CONFIG', tmp_path / 'rent.json')
    monkeypatch.setattr(rcmd, 'POLL_S', 0.0)
    monkeypatch.setenv('GT_API_URL', 'http://api.test')
    monkeypatch.setenv('GT_API_KEY', 'gt_testkey')
    (tmp_path / '.ssh').mkdir()
    (tmp_path / '.ssh' / 'id_ed25519.pub').write_text(KEY + '\n')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    return app


def invoke(*args):
    return CliRunner(env={'COLUMNS': '200'}).invoke(cli, ['rent', *args], catch_exceptions=False)


def test_ls_shows_only_free_sizes_with_runway_and_the_balance(product):
    r = invoke('ls')
    assert r.exit_code == 0, r.output
    assert 'RTX5090' in r.output and '0.65' in r.output and '15.1 h' in r.output  # $9.84 / $0.65
    assert 'H100' not in r.output and 'balance $9.84' in r.output
    assert 'H100' in invoke('ls', '--all').output
    assert json.loads(invoke('ls', '--json').output)['balance']['balance_cents'] == 984


def test_up_orders_with_the_keys_under_ssh_waits_for_active_and_prints_the_ssh_line(product):
    r = invoke('up', 'rtx5090', '-n', 'dev', '-p', '8888', '-e', 'HELLO=world')
    assert r.exit_code == 0, r.output + r.stderr
    body = product.orders[0]
    assert body == {
        'gpu_type': 'RTX5090',
        'gpu_count': 1,
        'hours': 1.0,
        'image': OFFERS['images'][0]['image'],
        'ssh_pubkeys': [KEY],
        'ports': [22, 8888],
        'env': {'HELLO': 'world'},
    }
    assert 'ssh root@203.0.113.7 -p 40047' in r.output and '8888 → 203.0.113.7:40045' in r.output
    assert 'dev (rnt_001)' in r.output
    assert 'requested' in r.stderr and 'active' in r.stderr  # the live state line
    assert rapi.RentConfig.load().names == {'dev': 'rnt_001'}


def test_up_refuses_up_front_when_nothing_of_that_size_is_free(product):
    r = invoke('up', 'RTX5090', '-c', '2')
    assert r.exit_code == 1 and 'no RTX5090 ×2 box is free right now' in r.stderr and product.orders == []
    r = invoke('up', 'RTX5090', '-c', '2', '--queue', '--no-wait')
    assert r.exit_code == 0 and product.orders[0]['gpu_count'] == 2 and 'rnt_001' in r.output
    r = invoke('up', 'A100')
    assert r.exit_code == 2 and 'no such box size: A100' in r.stderr  # usage: exit 2
    # a bad input is answered as such, before availability is even looked at (the agent got "nothing is free" for -H 0.1)
    r = invoke('up', 'RTX5090', '-c', '2', '-H', '0.1')
    assert r.exit_code == 2 and 'hours must be a number from 0.25 to 168' in r.stderr and 'free' not in r.stderr
    r = invoke('extend', 'rnt_001', '-0.25')  # a negative number is hours, not an option
    assert r.exit_code == 2 and 'hours must be a number' in r.stderr
    # a name that still points at an open rental is not silently taken over
    r = invoke('up', 'RTX5090', '-n', 'held', '--queue', '--no-wait')
    r = invoke('up', 'RTX5090', '-n', 'held', '--queue', '--no-wait')
    assert r.exit_code == 2 and "'held' is requested (rnt_002)" in r.stderr and len(product.orders) == 2


def test_up_retries_once_on_a_failed_start_and_explains_other_failures(product):
    product.script = ['requested', 'failed']
    r = invoke('up', 'RTX5090')
    assert r.exit_code == 1 and len(product.orders) == 2 and 'start failed once; ordering again' in r.stderr
    assert 'never answered on :22' in r.output
    product.orders.clear()
    product.fail_reason = 'pull_failed'  # the image: not retried, explained
    r = invoke('up', 'RTX5090', '-i', 'nobody/nothing:latest')
    assert r.exit_code == 1 and len(product.orders) == 1 and 'could not pull the image' in r.output
    r = invoke('up', 'RTX5090', '--json')
    assert r.exit_code == 1 and json.loads(r.output)['reason'] == 'pull_failed'


def test_up_without_a_key_file_or_login_says_what_to_do(product, monkeypatch, tmp_path):
    (tmp_path / '.ssh' / 'id_ed25519.pub').unlink()
    r = invoke('up', 'RTX5090')
    assert r.exit_code == 2 and 'no SSH public key' in r.stderr
    monkeypatch.delenv('GT_API_KEY')
    r = invoke('ls')
    assert r.exit_code == 2 and 'gitt rent login' in r.stderr and 'GITTENSOR_API_KEY' in r.stderr
    # the runbook's variable name works too, and wins over the short one
    monkeypatch.setenv('GITTENSOR_API_KEY', 'gt_testkey')
    monkeypatch.setenv('GT_API_KEY', 'gt_wrong')
    assert invoke('balance').exit_code == 0


def test_the_apis_own_refusal_is_printed_as_is(product):
    product.fail_order = {
        'status': 402,
        'message': 'balance $0.16 short of the 15-minute minimum',
        'type': 'insufficient_balance',
    }
    r = invoke('up', 'RTX5090')
    assert r.exit_code == 1 and 'balance $0.16 short' in r.stderr
    r = invoke('up', 'RTX5090', '--json')
    assert json.loads(r.output)['error'] == {
        'type': 'insufficient_balance',
        'message': 'balance $0.16 short of the 15-minute minimum',
    }


def test_ps_ssh_extend_and_rm_resolve_a_name_an_id_or_the_one_open_rental(product, monkeypatch):
    invoke('up', 'RTX5090', '-n', 'dev')
    r = invoke('ps')
    assert r.exit_code == 0 and 'dev' in r.output and 'rnt_001' in r.output and 'active' in r.output
    execs: list[list[str]] = []
    monkeypatch.setattr(rcmd.os, 'execvp', lambda prog, argv: execs.append(argv))
    assert invoke('ssh').exit_code == 0  # one open rental: no name needed
    assert invoke('ssh', 'dev', '--', 'nvidia-smi').exit_code == 0
    assert execs[0][-1] == 'root@203.0.113.7' and '-p' in execs[0] and '40047' in execs[0]
    assert execs[1][-2:] == ['root@203.0.113.7', 'nvidia-smi']
    assert invoke('ssh', '--', 'nvidia-smi', '-L').exit_code == 0  # no name, one open rental: all of it is the command
    assert execs[2][-3:] == ['root@203.0.113.7', 'nvidia-smi', '-L']
    # a typo before `--` is a typo, never a command run on the box (the agent's round-3 bug)
    r = invoke('ssh', 'nosuch', '--', 'true')
    assert r.exit_code == 2 and "no rental 'nosuch'" in r.stderr and len(execs) == 3
    r = invoke('ssh', 'dev', 'extra', '--', 'true')
    assert r.exit_code == 2 and 'usage: gitt rent ssh' in r.stderr
    # a scripted command is quiet on stderr; the interactive form shows the ssh line to reuse
    assert invoke('ssh', 'dev', '--', 'hostname').stderr == ''
    assert 'ssh -o' in invoke('ssh', 'dev').stderr
    r = invoke('extend', 'rnt_0', '2')
    assert r.exit_code == 0 and product.rentals['rnt_001']['ends_at'] == 5000 + 7200
    r = invoke('rm', 'dev')
    assert r.exit_code == 0, r.output + r.stderr
    assert 'ended dev (rnt_001)' in r.output and 'used 1 min' in r.output and 'billed $0.16' in r.output
    assert 'balance $9.84' in r.output
    r = invoke('ssh', 'nothing')
    assert r.exit_code == 2 and "no rental 'nothing'" in r.stderr
    r = invoke('rm')
    assert r.exit_code == 2 and 'no open rental' in r.stderr


def test_login_checks_the_key_and_saves_it(product, tmp_path, monkeypatch):
    monkeypatch.delenv('GT_API_KEY')
    r = invoke('login', 'gt_testkey', '--url', 'http://api.test/')
    assert r.exit_code == 0 and 'balance $9.84' in r.output
    saved = json.loads((tmp_path / 'rent.json').read_text())
    assert saved == {'url': 'http://api.test', 'key': 'gt_testkey', 'names': {}}
    assert oct((tmp_path / 'rent.json').stat().st_mode)[-3:] == '600'


def test_rm_says_when_the_bill_is_the_minimum_and_a_reason_reads_as_words(product):
    product.script = ['active']
    invoke('up', 'RTX5090', '-n', 'dev')
    r = invoke('rm', 'dev')
    # the fixture's rental ran 72 s and was billed 16 cents: the API's 15-minute minimum, said so
    assert r.exit_code == 0 and 'used 1 min' in r.output and 'billed $0.16 (the 15-minute minimum)' in r.output
    r = invoke('ps', '--all')
    assert 'stopped by you' in r.output  # customer_stop, as words


def test_an_edge_403_or_a_dead_url_says_to_check_the_url(monkeypatch, tmp_path):
    import urllib.error

    from gittensor.cli.rent_commands import api as rent_api

    monkeypatch.setenv('GITTENSOR_API_KEY', 'gt_testkey')
    monkeypatch.setenv('GT_API_URL', 'https://wrong.example')
    monkeypatch.setattr(rent_api, 'RENT_CONFIG', tmp_path / 'rent.json')

    def edge(req, timeout=None):  # Cloudflare's bare 403: HTML, no API error object
        raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', Message(), io.BytesIO(b'<html>blocked</html>'))

    monkeypatch.setattr('urllib.request.urlopen', edge)
    r = invoke('balance')
    assert r.exit_code == 1 and 'HTTP 403 from https://wrong.example' in r.stderr and '--url' in r.stderr

    def dead(req, timeout=None):
        raise urllib.error.URLError('Name or service not known')

    monkeypatch.setattr('urllib.request.urlopen', dead)
    r = invoke('balance')
    assert r.exit_code == 1 and 'unreachable' in r.stderr and '--url' in r.stderr
