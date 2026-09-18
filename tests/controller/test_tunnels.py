# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The tunnel keeper against a fake OpenSSH: one master per box, forwards added and cancelled on it live, local ports
kept across restarts, reconnects with a fresh certificate, per-box isolation, ``tunnels.json`` and the CLI."""

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.controller import cli as ctl
from gittensor.controller import tunnels
from gittensor.controller.checks.state import BoxState, StateStore
from gittensor.controller.reconcile import InstanceRecord, InstanceStore
from gittensor.controller.ssh.certs import VisitCredential
from gittensor.controller.tunnels import TunnelKeeper, TunnelRunner, forward_spec, key_id_for

BRIDGE = '172.17.0.1'
LISTEN = '127.0.0.1'


def _option(argv, name):
    for arg in argv:
        if arg.startswith(f'{name}='):
            return arg.split('=', 1)[1]
    return ''


class FakeCA:
    def __init__(self):
        self.minted = 0

    def mint(self, key_id, principal='root', validity_s=300, source_address=None):
        self.minted += 1
        workdir = Path(tempfile.mkdtemp(prefix='gt-visit-test-'))
        key, cert = workdir / 'id_ed25519', workdir / 'id_ed25519-cert.pub'
        key.write_text('key')
        cert.write_text(f'cert {self.minted}')
        now = time.time()
        return VisitCredential(key, cert, key_id, principal, now - 60, now + validity_s)


class FakeMaster:
    def __init__(self, host, cert, returncode=None):
        self.host, self.cert, self.returncode = host, cert, returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


class FakeSsh:
    """OpenSSH as the keeper sees it: masters (``popen``), ``-O`` requests and commands over a master (``run``), and
    HTTP through a local port (``probe``)."""

    def __init__(self):
        self.masters: dict[str, FakeMaster] = {}  # control path -> master
        self.forwards: dict[str, set[str]] = {}  # control path -> forward specs registered on that master
        self.unreachable: set[str] = set()
        self.blocked: dict[str, threading.Event] = {}
        self.ports_in_use: set[int] = set()
        self.silent_ports: set[int] = set()  # the forward is there but the workload behind it does not answer
        self.cut: set[int] = set()  # id() of masters still running locally whose connection no longer answers
        self.link_checks: list[str] = []  # host of every link check
        self.connects: list[tuple[str, str]] = []  # (host, certificate text)
        self.ops: list[tuple[str, str, str]] = []  # (host, op, forward spec)
        self.on_probe = None
        self.lock = threading.Lock()

    def popen(self, argv, stdin=None, stdout=None, stderr=None):
        host = argv[-1].split('@', 1)[1]
        control = _option(argv, 'ControlPath')
        if host in self.blocked:
            self.blocked[host].wait(10)
        cert = Path(_option(argv, 'CertificateFile')).read_text()
        with self.lock:
            self.connects.append((host, cert))
            if host in self.unreachable:
                stderr.write(f'ssh: connect to host {host} port 2200: Connection timed out\n'.encode())
                return FakeMaster(host, cert, returncode=255)
            master = FakeMaster(host, cert)
            self.masters[control], self.forwards[control] = master, set()
            return master

    def alive(self, control):
        master = self.masters.get(control)
        return master is not None and master.returncode is None

    def run(self, argv, input=None, capture_output=True, timeout=None):
        control = _option(argv, 'ControlPath')
        host = argv[-1].split('@', 1)[1] if '-O' in argv else argv[argv.index('--') - 1].split('@', 1)[1]
        with self.lock:
            alive = self.alive(control)
            if '-O' not in argv:
                assert 'ControlMaster=no' in argv, 'a command must ride the master, never log in itself'
                if argv[-1] == 'true':
                    assert 'ProxyCommand=false' in argv and not _option(argv, 'CertificateFile')
                    self.link_checks.append(host)
                    if id(self.masters.get(control)) in self.cut:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    if not alive:
                        return subprocess.CompletedProcess(argv, 255, b'', b'Control socket connect: No such file\n')
                    return subprocess.CompletedProcess(argv, 0, b'', b'')
                if not alive:
                    return subprocess.CompletedProcess(argv, 255, b'', b'Permission denied (publickey).\n')
                return subprocess.CompletedProcess(argv, 0, f'{BRIDGE}\n'.encode(), b'')
            op = argv[argv.index('-O') + 1]
            spec = argv[argv.index('-L') + 1] if '-L' in argv else ''
            self.ops.append((host, op, spec))
            if not alive:
                return subprocess.CompletedProcess(argv, 255, b'', b'Control socket connect: No such file\n')
            if op == 'forward':
                if int(spec.split(':')[1]) in self.ports_in_use:
                    return subprocess.CompletedProcess(argv, 255, b'', b'Port forwarding failed\n')
                self.forwards[control].add(spec)
            elif op == 'cancel':
                self.forwards[control].discard(spec)
            elif op == 'exit':
                self.masters[control].returncode = 0
            return subprocess.CompletedProcess(argv, 0, b'', b'')

    def cut_link(self, host):
        """The connection goes silent: ssh has not noticed yet, the master still runs."""
        self.cut |= {id(m) for m in self.masters.values() if m.host == host and m.returncode is None}

    def kill_master(self, host):
        for master in self.masters.values():
            if master.host == host and master.returncode is None:
                master.returncode = 255

    def probe(self, host, port):
        if self.on_probe is not None:
            self.on_probe()
        with self.lock:
            for control, specs in self.forwards.items():
                if self.alive(control) and any(s.split(':')[:2] == [host, str(port)] for s in specs):
                    if port in self.silent_ports:
                        return False, 'no HTTP status line: connection closed'
                    return True, ''
        return False, 'no answer: [Errno 111] Connection refused'

    def ops_for(self, host, op):
        return [spec for h, o, spec in self.ops if h == host and o == op]


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def world(tmp_path):
    ssh, ca, clock, events = FakeSsh(), FakeCA(), Clock(), []
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'known_hosts').write_text('')

    def make_runner(box, control):
        return TunnelRunner(
            box.host, box.port, ca, state / 'known_hosts', key_id_for(box.box_id), control=control, run=ssh.run
        )

    def keeper(**kw):
        return TunnelKeeper(
            state,
            make_runner,
            listen_host=LISTEN,
            port_range=kw.pop('port_range', (21000, 21009)),
            popen=ssh.popen,
            probe=ssh.probe,
            emit=events.append,
            clock=clock,
            control_root=tmp_path / 'ctl',
            **kw,
        )

    class World:
        pass

    w = World()
    w.ssh, w.ca, w.clock, w.events, w.state, w.keeper = ssh, ca, clock, events, state, keeper
    w.boxes = lambda **hosts: _boxes(state, hosts)
    w.instances = lambda *rows: _instances(state, rows)
    w.doc = lambda: json.loads((state / 'tunnels.json').read_text())
    w.kinds = lambda kind: [e for e in events if e['kind'] == kind]
    return w


def _boxes(state, hosts):
    store = StateStore(state / 'boxes.json')
    for box_id, host in hosts.items():
        store.put(BoxState(box_id, host=host, port=2200))


def _instances(state, rows):
    path = state / 'instances.json'
    path.unlink(missing_ok=True)
    store = InstanceStore(path)
    for instance_id, box, host_port in rows:
        store.put(InstanceRecord(instance_id, 'qwen@7', box, f'GPU-{instance_id}', host_port=host_port))
    store.save()


def _up(doc):
    return {i: row['up'] for i, row in doc['tunnels'].items()}


# ---------------------------------------------------------------- forwards on the live master -------------------------


def test_forwards_are_added_and_cancelled_on_the_live_master(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', 20001))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    assert _up(world.doc()) == {'i1': True, 'i2': True}
    assert len(world.ssh.connects) == 1
    master = next(iter(world.ssh.masters.values()))
    p1, p2 = keeper.ports['i1'], keeper.ports['i2']
    assert world.ssh.ops_for('10.0.0.1', 'forward') == [
        forward_spec(LISTEN, p1, BRIDGE, 20000),
        forward_spec(LISTEN, p2, BRIDGE, 20001),
    ]

    world.instances(('i2', 'boxA', 20001), ('i3', 'boxA', 20002))  # i1's card cycled, i3 started
    keeper.pass_once(wait_s=5)
    assert _up(world.doc()) == {'i2': True, 'i3': True}
    assert len(world.ssh.connects) == 1 and master.returncode is None  # same connection throughout
    assert world.ssh.ops_for('10.0.0.1', 'cancel') == [forward_spec(LISTEN, p1, BRIDGE, 20000)]
    assert world.ssh.ops_for('10.0.0.1', 'forward')[-1] == forward_spec(LISTEN, keeper.ports['i3'], BRIDGE, 20002)
    assert keeper.ports['i2'] == p2
    assert [e['instance'] for e in world.kinds('cancel')] == ['i1']

    world.instances()  # the box's last instance gone: its connection closes
    keeper.pass_once(wait_s=5)
    assert world.doc()['tunnels'] == {}
    assert master.returncode is not None
    keeper.shutdown()


def test_a_failed_forward_leaves_the_other_instances_up(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', 20001))
    world.ssh.ports_in_use.add(21001)  # the port i2 is handed
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    doc = world.doc()['tunnels']
    assert doc['i1']['up'] is True
    assert doc['i2']['up'] is False and 'forward failed: Port forwarding failed' in doc['i2']['error']
    keeper.pass_once(wait_s=5)  # retried every pass, reported once
    assert len([e for e in world.kinds('forward') if not e['ok']]) == 1
    world.ssh.ports_in_use.clear()
    keeper.pass_once(wait_s=5)
    assert _up(world.doc()) == {'i1': True, 'i2': True}
    keeper.shutdown()


def test_up_needs_an_http_status_line_through_the_port(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000))
    world.ssh.silent_ports.add(21000)
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    row = world.doc()['tunnels']['i1']
    assert row['up'] is False and row['error'] == 'no HTTP status line: connection closed'
    world.ssh.silent_ports.clear()
    keeper.pass_once(wait_s=5)
    assert world.doc()['tunnels']['i1']['up'] is True
    keeper.shutdown()


# ---------------------------------------------------------------- local ports -------------------------------------------


def test_local_ports_survive_a_keeper_restart_and_gone_instances_release_theirs(world):
    world.boxes(boxA='10.0.0.1', boxB='10.0.0.2')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxB', 20000), ('i3', 'boxB', 20001))
    first = world.keeper()
    first.pass_once(wait_s=5)
    ports = dict(first.ports)
    assert sorted(ports.values()) == [21000, 21001, 21002]
    first.shutdown()
    assert not any(row['up'] for row in world.doc()['tunnels'].values())

    world.instances(('i2', 'boxB', 20000), ('i3', 'boxB', 20001), ('i4', 'boxA', 20001))  # i1 gone meanwhile
    second = world.keeper()
    second.pass_once(wait_s=5)
    doc = world.doc()['tunnels']
    assert doc['i2']['port'] == ports['i2'] and doc['i3']['port'] == ports['i3']
    assert 'i1' not in doc and 'i1' not in second.ports
    assert doc['i4']['port'] not in (ports['i2'], ports['i3'])
    assert doc['i4']['port'] != ports['i1']  # a released port is the last to be handed out again
    assert _up(world.doc()) == {'i2': True, 'i3': True, 'i4': True}
    second.shutdown()


def test_port_range_exhausted_is_an_error_not_a_shared_port(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', 20001))
    keeper = world.keeper(port_range=(21000, 21000))
    keeper.pass_once(wait_s=5)
    doc = world.doc()['tunnels']
    assert doc['i1'] == {**doc['i1'], 'port': 21000, 'up': True}
    assert doc['i2']['port'] is None and doc['i2']['error'] == 'no free local port in --port-range'
    keeper.shutdown()


# ---------------------------------------------------------------- reconnects ----------------------------------------------


def test_a_dead_master_takes_its_instances_down_and_reconnects_with_a_new_certificate(world):
    world.boxes(boxA='10.0.0.1', boxB='10.0.0.2')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', 20001), ('i3', 'boxB', 20000))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    assert all(_up(world.doc()).values())
    since = world.doc()['tunnels']['i1']['since']

    world.ssh.kill_master('10.0.0.1')
    world.ssh.unreachable.add('10.0.0.1')
    world.clock.now += 10
    keeper.pass_once(wait_s=5)
    doc = world.doc()['tunnels']
    assert (doc['i1']['up'], doc['i2']['up'], doc['i3']['up']) == (False, False, True)
    assert doc['i1']['error'].startswith('connect failed: ssh exited 255: ssh: connect to host 10.0.0.1')
    assert doc['i1']['since'] == since + 10
    assert {e['instance'] for e in world.kinds('down') if 'instance' in e} == {'i1', 'i2'}
    assert [e['box'] for e in world.kinds('down') if 'instances' in e] == ['boxA']

    world.ssh.unreachable.clear()
    keeper.pass_once(wait_s=5)  # still inside the backoff: no attempt
    assert len([c for c in world.ssh.connects if c[0] == '10.0.0.1']) == 2
    world.clock.now += 1
    keeper.pass_once(wait_s=5)
    assert all(_up(world.doc()).values())
    certs = [cert for host, cert in world.ssh.connects if host == '10.0.0.1']
    assert len(certs) == 3 and len(set(certs)) == 3  # a freshly minted certificate for every connect
    assert world.ca.minted == 4  # boxA x3, boxB x1
    keeper.shutdown()


def test_two_failed_link_checks_in_a_row_reconnect_and_write_the_box_down_at_once(world):
    world.boxes(boxA='10.0.0.1', boxB='10.0.0.2')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', 20001), ('i3', 'boxB', 20000))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    assert all(_up(world.doc()).values()) and world.ssh.link_checks == []  # a fresh connection is not checked
    old = next(m for m in world.ssh.masters.values() if m.host == '10.0.0.1')

    world.ssh.cut_link('10.0.0.1')
    keeper.pass_once(wait_s=5)  # one failure only counts
    assert all(_up(world.doc()).values()) and old.returncode is None
    assert [(e['box'], e['failures']) for e in world.kinds('link_check')] == [('boxA', 1)]

    world.events.clear()
    keeper.pass_once(wait_s=5)
    assert old.terminated
    assert len([h for h, _ in world.ssh.connects if h == '10.0.0.1']) == 2  # reconnected in the same pass
    assert _up(world.doc()) == {'i1': True, 'i2': True, 'i3': True}
    kinds = [(e['kind'], e.get('instance', e.get('box'))) for e in world.events if e['kind'] in ('down', 'connect')]
    assert kinds == [('down', 'boxA'), ('down', 'i1'), ('down', 'i2'), ('connect', 'boxA')]  # down before reconnect
    assert 'link checks failed in a row' in world.kinds('down')[1]['detail']
    assert len([h for h, _ in world.ssh.connects if h == '10.0.0.2']) == 1  # boxB's connection untouched
    keeper.shutdown()


def test_a_workload_that_stops_answering_never_costs_its_box_the_connection(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', 20001))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    master = next(iter(world.ssh.masters.values()))
    world.ssh.silent_ports.add(keeper.ports['i1'])  # i1's runtime is starting, or stopped
    for _ in range(4):
        keeper.pass_once(wait_s=5)
    assert _up(world.doc()) == {'i1': False, 'i2': True}
    assert len(world.ssh.connects) == 1 and master.returncode is None
    assert world.ssh.link_checks == ['10.0.0.1'] * 4 and not world.kinds('link_check')
    assert world.ssh.ops_for('10.0.0.1', 'cancel') == []  # i2's forward untouched throughout
    keeper.shutdown()


def test_link_check_argv_rides_the_master_only(world, tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    runner = TunnelRunner('10.0.0.1', 2200, world.ca, tmp_path / 'kh', 'tun-x', control=tmp_path / 'c', run=run)
    assert runner.link_check().ok and world.ca.minted == 0  # no certificate: nothing logs in
    argv, kw = calls[0]
    assert argv[argv.index('--') :] == ['--', 'true'] and kw['timeout'] == tunnels.LINK_CHECK_TIMEOUT_S
    assert {'ControlMaster=no', f'ControlPath={tmp_path / "c"}', 'ProxyCommand=false', 'BatchMode=yes'} <= set(argv)


def test_connect_failures_back_off_to_the_cap(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000))
    world.ssh.unreachable.add('10.0.0.1')
    keeper = world.keeper()
    delays = []
    for _ in range(8):
        keeper.pass_once(wait_s=5)
        delays.append(keeper.links['boxA'].next_attempt - world.clock.now)
        world.clock.now = keeper.links['boxA'].next_attempt
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert [e['attempt'] for e in world.kinds('connect_failed')] == list(range(1, 9))
    keeper.shutdown()


def test_one_unreachable_box_does_not_hold_up_another(world):
    world.boxes(boxA='10.0.0.1', boxB='10.0.0.2')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxB', 20000))
    world.ssh.blocked['10.0.0.2'] = threading.Event()  # boxB's login hangs
    keeper = world.keeper()
    started = time.monotonic()
    keeper.pass_once(wait_s=0.5)
    assert time.monotonic() - started < 2
    doc = world.doc()['tunnels']
    assert doc['i1']['up'] is True
    assert doc['i2'] == {**doc['i2'], 'up': False, 'error': 'pending'}
    keeper.pass_once(wait_s=0.2)  # boxB still busy: not handed out twice, boxA served again
    assert len([h for h, _ in world.ssh.connects if h == '10.0.0.2']) == 0
    assert world.doc()['tunnels']['i1']['up'] is True
    world.ssh.blocked['10.0.0.2'].set()
    deadline = time.monotonic() + 5
    while 'boxB' in keeper._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    keeper.pass_once(wait_s=5)
    assert _up(world.doc()) == {'i1': True, 'i2': True}
    keeper.shutdown()


def test_instance_on_a_box_not_in_boxes_json(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'gone', 20000))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    doc = world.doc()['tunnels']
    assert doc['i1']['up'] is True
    assert doc['i2']['up'] is False and doc['i2']['error'] == 'box not in boxes.json'
    keeper.shutdown()


def test_an_unreadable_instances_file_keeps_the_last_view(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    (world.state / 'instances.json').write_text('{"i1": {"id": ')
    keeper.pass_once(wait_s=5)
    assert _up(world.doc()) == {'i1': True}
    assert world.ssh.ops_for('10.0.0.1', 'cancel') == []
    assert world.kinds('read_failed')
    keeper.shutdown()


def test_a_socket_left_by_an_earlier_keeper_is_cleared_before_connecting(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    control = next(iter(world.ssh.masters))
    Path(control).touch()  # what a keeper killed without closing leaves behind
    keeper.links.clear()  # the new keeper knows nothing of it
    keeper.pass_once(wait_s=5)
    assert world.ssh.ops_for('10.0.0.1', 'exit') == ['']
    assert _up(world.doc()) == {'i1': True}
    keeper.shutdown()


# ---------------------------------------------------------------- the ssh argv --------------------------------------------


def test_master_argv_is_a_foreground_master_with_the_pinned_host_key(world, tmp_path):
    calls = []
    runner = TunnelRunner(
        '10.0.0.1',
        2200,
        world.ca,
        tmp_path / 'known_hosts',
        key_id_for('5Hot key'),
        control=tmp_path / 'cm',
        run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b'', b''),
    )
    argv = runner.master_argv()
    assert argv[0] == 'ssh' and '-N' in argv and argv[-3:] == ['-p', '2200', 'root@10.0.0.1']
    for option in (
        'ControlMaster=yes',
        f'ControlPath={tmp_path / "cm"}',
        'ControlPersist=no',
        'BatchMode=yes',
        'StrictHostKeyChecking=yes',
        f'UserKnownHostsFile={tmp_path / "known_hosts"}',
        'ServerAliveInterval=15',
        'ServerAliveCountMax=3',
        'ExitOnForwardFailure=yes',
        'IdentitiesOnly=yes',
    ):
        assert option in argv, option
    assert 'ControlMaster=no' not in argv and 'ControlMaster=auto' not in argv
    assert _option(argv, 'CertificateFile').endswith('id_ed25519-cert.pub')
    assert key_id_for('5Hot key') == 'tun-5Hot_key'

    runner.control('forward', forward_spec('172.19.0.1', 21003, BRIDGE, 20000))
    assert calls[-1] == [
        'ssh',
        '-o',
        f'ControlPath={tmp_path / "cm"}',
        '-O',
        'forward',
        '-L',
        '172.19.0.1:21003:172.17.0.1:20000',
        '-p',
        '2200',
        'root@10.0.0.1',
    ]
    runner.run('true')
    assert 'ControlMaster=no' in calls[-1] and '-N' not in calls[-1]
    runner.close()


# ---------------------------------------------------------------- tunnels.json ------------------------------------------


def test_tunnels_json_shape(world):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', None))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    doc = world.doc()
    assert doc == {
        'schema': 1,
        'written_at': world.clock.now,
        'listen_host': LISTEN,
        'tunnels': {
            'i1': {'box': 'boxA', 'host': LISTEN, 'port': 21000, 'up': True, 'since': world.clock.now, 'error': ''},
            'i2': {
                'box': 'boxA',
                'host': LISTEN,
                'port': 21001,
                'up': False,
                'since': world.clock.now,
                'error': 'no host port on the instance record',
            },
        },
    }
    keeper.shutdown()


def test_tunnels_json_is_replaced_whole(world, monkeypatch):
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000))
    seen = []
    real_replace = os.replace

    def replace(src, dst):
        seen.append((Path(src), Path(dst), json.loads(Path(src).read_text())))
        real_replace(src, dst)

    monkeypatch.setattr(tunnels.os, 'replace', replace)
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    keeper.shutdown()
    assert seen
    for src, dst, doc in seen:
        assert dst == world.state / 'tunnels.json' and src.parent == dst.parent and src != dst
        assert doc['schema'] == 1 and set(doc) == {'schema', 'written_at', 'listen_host', 'tunnels'}
    assert [p.name for p in world.state.iterdir() if p.name.endswith('.tmp')] == []


# ---------------------------------------------------------------- the command ---------------------------------------------


def test_sigterm_closes_every_master_and_writes_everything_down(world, monkeypatch):
    world.boxes(boxA='10.0.0.1', boxB='10.0.0.2')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxB', 20000))
    (world.state / 'gt_ca').write_text('ca')
    fired = []

    def sigterm_once():
        if not fired:
            fired.append(True)
            os.kill(os.getpid(), signal.SIGTERM)

    world.ssh.on_probe = sigterm_once
    monkeypatch.setattr(ctl, '_make_keeper', lambda state, ca_key, listen_host, ports: world.keeper())
    before = signal.getsignal(signal.SIGTERM)
    result = CliRunner().invoke(
        cli, ['controller', 'tunnels', '--state-dir', str(world.state), '--interval', '0.05', '--max-passes', '200']
    )
    assert result.exit_code == 0, result.output
    assert signal.getsignal(signal.SIGTERM) == before
    assert fired
    assert world.ssh.masters and all(m.returncode is not None for m in world.ssh.masters.values())
    assert sorted(h for h, op, _ in world.ssh.ops if op == 'exit') == ['10.0.0.1', '10.0.0.2']
    doc = world.doc()['tunnels']
    assert {i: (r['up'], r['error']) for i, r in doc.items()} == {
        'i1': (False, 'keeper stopped'),
        'i2': (False, 'keeper stopped'),
    }
    assert world.kinds('stop') and world.kinds('start')
    assert not list((world.state.parent / 'ctl').glob('*.err'))


def test_a_second_keeper_on_the_same_state_dir_refuses(world, tmp_path):
    (world.state / 'gt_ca').write_text('ca')
    with tunnels.keeper_lock(world.state):
        result = CliRunner().invoke(cli, ['controller', 'tunnels', '--state-dir', str(world.state), '--json'])
    assert result.exit_code == 2
    assert 'already runs' in json.loads(result.output)['error']['message']


def test_status_reads_tunnels_json(world):
    runner = CliRunner()
    missing = runner.invoke(cli, ['controller', 'tunnels', '--status', '--state-dir', str(world.state)])
    assert missing.exit_code == 2
    world.boxes(boxA='10.0.0.1')
    world.instances(('i1', 'boxA', 20000), ('i2', 'boxA', None))
    keeper = world.keeper()
    keeper.pass_once(wait_s=5)
    keeper.shutdown()
    as_json = runner.invoke(cli, ['controller', 'tunnels', '--status', '--json', '--state-dir', str(world.state)])
    assert as_json.exit_code == 0
    payload = json.loads(as_json.output)
    assert payload['success'] is True and set(payload['tunnels']) == {'i1', 'i2'}
    table = runner.invoke(cli, ['controller', 'tunnels', '--status', '--state-dir', str(world.state)])
    assert table.exit_code == 0
    assert 'i1' in table.output and 'down' in table.output and 'keeper stopped' in table.output


def test_bad_port_range(world):
    result = CliRunner().invoke(
        cli, ['controller', 'tunnels', '--state-dir', str(world.state), '--port-range', '22000-21000']
    )
    assert result.exit_code == 2 and 'LOW <= HIGH' in result.output
