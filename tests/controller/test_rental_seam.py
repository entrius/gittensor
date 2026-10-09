# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The order seam (vault 29 §3) as the controller drives it: orders pulled into the store, status pushed back once per
transition, the app's readings honoured (poll includes active for extensions; an ending we never placed is answered
ended; a lost report is retried, a repeated one is harmless)."""

import io
import json
import urllib.error
from email.message import Message

import pytest

from gittensor.controller import rentals as rt
from gittensor.controller.rental_seam import POLL_STATES, RentalPoller, SeamClient, SeamError, report_body

NOW = 1_760_000_000.0
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests0000000000000000000000000 alex'


class FakeApp:
    """gittensor-app's two seam routes, in memory: what the controller would see."""

    def __init__(self, orders=None, fail_patch: bool = False):
        self.orders = list(orders or [])
        self.patches: list[tuple[str, dict]] = []
        self.calls: list[str] = []
        self.fail_patch = fail_patch

    def __call__(self, req, timeout=None):
        self.calls.append(f'{req.get_method()} {req.full_url}')
        assert req.get_header('Authorization') == 'Bearer secret-token'
        if req.get_method() == 'GET':
            states = req.full_url.split('state=')[1].split(',')
            body = {'rentals': [o for o in self.orders if o['state'] in states]}
            return _Resp(json.dumps(body).encode())
        rid = req.full_url.rsplit('/', 1)[1]
        if self.fail_patch:
            raise urllib.error.HTTPError(req.full_url, 503, 'down', Message(), io.BytesIO(b'maintenance'))
        self.patches.append((rid, json.loads(req.data)))
        return _Resp(json.dumps({'id': rid, 'state': 'whatever'}).encode())


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def order(rid='rnt_app1', state='requested', **kw) -> dict:
    o = {
        'id': rid,
        'state': state,
        'gpu_type': 'RTX5090',
        'gpu_count': 2,
        'box_uid': None,
        'image': 'ubuntu:24.04',
        'ssh_pubkeys': [KEY],
        'ports': [22, 8888],
        'env': {'A': '1'},
        'ends_at': NOW + 3600,
    }
    o.update(kw)
    return o


def poller(tmp_path, app: FakeApp) -> tuple[rt.RentalStore, RentalPoller]:
    store = rt.RentalStore(tmp_path / 'rentals.json')
    client = SeamClient('http://app.test/', 'secret-token', opener=app)
    return store, RentalPoller(store, client, wall=lambda: NOW)


def test_the_poll_asks_for_requested_ending_and_active(tmp_path):
    app = FakeApp()
    store, p = poller(tmp_path, app)
    assert POLL_STATES == ('requested', 'ending', 'active')
    assert p.take_orders().seen == 0
    assert app.calls == ['GET http://app.test/internal/rentals?state=requested,ending,active']


def test_an_order_pinned_by_hotkey_carries_the_pin(tmp_path):
    """The rent page's RENT NOW: ``box_hotkey`` on the order is the pin (box listing, 10/9); ``box_uid`` still works."""
    app = FakeApp([order(box_hotkey='5Box'), order(rid='rnt_app2', box_uid=45)])
    store, p = poller(tmp_path, app)
    assert p.take_orders().placed == ['rnt_app1', 'rnt_app2']
    assert store.rentals['rnt_app1'].want_box_hotkey == '5Box' and store.rentals['rnt_app1'].pinned
    assert store.rentals['rnt_app2'].want_box_uid == 45 and store.rentals['rnt_app2'].want_box_hotkey == ''
    assert not store.rentals['rnt_app1'].want_box_uid


def test_a_requested_order_becomes_a_record_under_the_apps_id(tmp_path):
    app = FakeApp([order()])
    store, p = poller(tmp_path, app)
    report = p.take_orders()
    assert report.placed == ['rnt_app1'] and report.ok
    r = store.rentals['rnt_app1']
    assert (r.state, r.gpu_type, r.gpu_count, r.image) == (rt.REQUESTED, 'RTX5090', 2, 'ubuntu:24.04')
    assert r.ssh_pubkeys == [KEY] and r.ports == [22, 8888] and r.env == {'A': '1'} and r.ends_at == NOW + 3600
    assert r.created_at == NOW and r.reported_state == ''
    # seen again next poll: nothing happens
    assert p.take_orders().placed == [] and len(store.rentals) == 1


def test_an_ending_we_never_placed_is_answered_ended_and_ours_is_ended(tmp_path):
    app = FakeApp([order('rnt_gone', state='ending')])
    store, p = poller(tmp_path, app)
    report = p.take_orders()
    assert report.ended == ['rnt_gone'] and store.rentals['rnt_gone'].state == rt.ENDED
    sent = p.send_reports()
    assert sent.reported == ['rnt_gone'] and app.patches[0][1]['state'] == 'ended'
    # one of ours, active: the order ends it (the pass drains the pod)
    ours = rt.place_order(
        store, gpu_type='RTX5090', gpu_count=1, image='x', ssh_pubkeys=[KEY], hours=1, rental_id='rnt_ours', now=NOW
    )
    ours.state, ours.box, ours.container_id = rt.ACTIVE, '5Box', 'c' * 64
    store.put(ours)
    app.orders = [order('rnt_ours', state='ending')]
    assert p.take_orders().ended == ['rnt_ours'] and store.rentals['rnt_ours'].state == rt.ENDING
    assert store.rentals['rnt_ours'].reason == 'app_ending'


def test_an_extension_moves_ends_at_on_an_active_rental(tmp_path):
    store, p = poller(tmp_path, FakeApp([order()]))
    p.take_orders()
    r = store.rentals['rnt_app1']
    r.state = rt.ACTIVE
    store.put(r)
    p.client._open.orders = [order(state='active', ends_at=NOW + 7200)]
    assert p.take_orders().extended == ['rnt_app1'] and store.rentals['rnt_app1'].ends_at == NOW + 7200
    assert p.take_orders().extended == []  # unchanged: nothing to do


def test_each_transition_is_reported_once_and_a_lost_report_is_retried(tmp_path):
    app = FakeApp([order()])
    store, p = poller(tmp_path, app)
    p.take_orders()
    assert p.send_reports().reported == []  # requested is the app's own state: nothing to say yet
    r = store.rentals['rnt_app1']
    r.state, r.box, r.box_uid, r.uuids, r.uuid = rt.STARTING_R, '5Box', 45, ['GPU-a', 'GPU-b'], 'GPU-a'
    r.host, r.port_map = '203.0.113.7', {'22': 31000, '8888': 31001}
    store.put(r)
    assert p.send_reports().reported == ['rnt_app1']
    rid, body = app.patches[-1]
    assert rid == 'rnt_app1' and body == {
        'state': 'starting',
        'box_uid': 45,
        'box_hotkey': '5Box',
        'host': '203.0.113.7',
        'ports': {'22': 31000, '8888': 31001},
        'gpu_uuids': ['GPU-a', 'GPU-b'],
        'started_at': None,
        'ended_at': None,
        'reason': '',
    }
    assert p.send_reports().reported == [] and len(app.patches) == 1  # told once
    r.state, r.started_at = rt.ACTIVE, NOW + 30
    store.put(r)
    app.fail_patch = True
    sent = p.send_reports()
    assert (
        sent.reported == [] and 'HTTP 503' in sent.errors[0] and store.rentals['rnt_app1'].reported_state == 'starting'
    )
    app.fail_patch = False
    assert p.send_reports().reported == ['rnt_app1'] and app.patches[-1][1]['started_at'] == int(NOW + 30)
    r.state, r.ended_at, r.reason = rt.ENDED, NOW + 3000, 'ends_at'
    store.put(r)
    p.send_reports()
    assert app.patches[-1][1]['state'] == 'ended' and app.patches[-1][1]['ended_at'] == int(NOW + 3000)
    assert app.patches[-1][1]['reason'] == 'ends_at'


def test_a_down_app_is_one_error_not_a_crash(tmp_path):
    def down(req, timeout=None):
        raise urllib.error.URLError('connection refused')

    store, p = poller(tmp_path, FakeApp())
    p.client._open = down
    status = p.poll()
    assert not status['ok'] and 'connection refused' in status['orders']['errors'][0]
    with pytest.raises(SeamError):
        p.client.orders()


def test_an_order_or_an_extension_past_the_longest_rental_is_refused(tmp_path):
    from gittensor.controller.checks import config as cfg

    too_long = order('rnt_long', ends_at=NOW + (cfg.RENTAL_MAX_HOURS + 1) * 3600)
    store, p = poller(tmp_path, FakeApp([too_long, order()]))
    report = p.take_orders()
    assert report.placed == ['rnt_app1'] and 'rnt_long' not in store.rentals
    assert 'rnt_long' in report.errors[0] and 'more than 168 h ahead' in report.errors[0]
    r = store.rentals['rnt_app1']
    r.state = rt.ACTIVE
    store.put(r)
    p.client._open.orders = [order(state='active', ends_at=NOW + (cfg.RENTAL_MAX_HOURS + 1) * 3600)]
    report = p.take_orders()
    assert report.extended == [] and 'not extended' in report.errors[0]
    assert store.rentals['rnt_app1'].ends_at == NOW + 3600  # ours stands
    p.client._open.orders = [order(state='active', ends_at=NOW + cfg.RENTAL_MAX_HOURS * 3600)]
    assert p.take_orders().extended == ['rnt_app1']  # exactly the longest: fine


def test_a_bad_order_is_skipped_with_an_error_and_the_rest_are_taken(tmp_path):
    bad = order('rnt_bad', ends_at=NOW - 1)  # already over
    store, p = poller(tmp_path, FakeApp([bad, order('rnt_ok')]))
    report = p.take_orders()
    assert report.placed == ['rnt_ok'] and 'rnt_bad' in report.errors[0] and 'rnt_bad' not in store.rentals


def test_an_end_the_app_ordered_keeps_the_apps_reason():
    r = rt.RentalRecord('rnt_x', state=rt.ENDED, reason='app_ending', ended_at=NOW)
    assert report_body(r)['reason'] == ''
    assert report_body(rt.RentalRecord('rnt_y', state=rt.ENDED, reason='ends_at', ended_at=NOW))['reason'] == 'ends_at'


def test_report_body_is_the_section_3_shape():
    r = rt.RentalRecord('rnt_x', state=rt.FAILED, reason=rt.NO_BOX_FITS, ended_at=NOW)
    assert report_body(r) == {
        'state': 'failed',
        'box_uid': None,
        'box_hotkey': None,
        'host': None,
        'ports': None,
        'gpu_uuids': None,
        'started_at': None,
        'ended_at': int(NOW),
        'reason': 'no_box_fits',
    }
