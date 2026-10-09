# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Where a box is (host specs, 10/9): the keyless lookup, the weekly cache on the box record, the per-pass cap,
and the ``--country`` placement that reads it."""

import dataclasses
import io
import json

from gittensor.controller import geo
from gittensor.controller import rentals as rt
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import BoxState, StateStore
from tests.controller.test_rental_seam import FakeApp, order, poller
from tests.controller.test_rentals import HK, NOW, rentable_box

HK_B = '5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty'


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_with(doc):
    calls = []

    def opener(req, timeout=None):
        calls.append((req.full_url, timeout))
        if isinstance(doc, Exception):
            raise doc
        return _Resp(json.dumps(doc).encode())

    return opener, calls


def test_the_lookup_reads_the_provider_and_fails_to_none_without_raising():
    opener, calls = opener_with({'status': 'success', 'countryCode': 'us', 'regionName': ' Texas ', 'city': 'Dallas'})
    assert geo.lookup_ip('8.8.8.8', opener) == {'country': 'US', 'region': 'Texas', 'city': 'Dallas'}
    assert calls == [(cfg.GEO_URL.format(ip='8.8.8.8'), cfg.GEO_TIMEOUT_S)]
    assert geo.lookup_ip('8.8.8.8', opener_with({'status': 'fail', 'message': 'reserved range'})[0]) is None
    assert geo.lookup_ip('8.8.8.8', opener_with(OSError('timed out'))[0]) is None
    assert geo.lookup_ip('8.8.8.8', opener_with({'status': 'success', 'countryCode': 'USA'})[0]) is None
    assert geo.lookup_ip('8.8.8.8', opener_with(['not', 'a', 'dict'])[0]) is None
    # a private address or a hostname is never sent to a third party
    for ip in ('10.0.0.1', '192.168.1.9', '127.0.0.1', 'localhost', 'box.example'):
        opener, calls = opener_with({'status': 'success', 'countryCode': 'US'})
        assert geo.lookup_ip(ip, opener) is None and calls == []


def test_a_box_is_looked_up_once_refreshed_weekly_and_a_miss_is_retried_after_a_day():
    fresh = BoxState(HK, host='8.8.8.8')
    assert geo.location_due(fresh, NOW)
    assert not geo.location_due(BoxState(HK_B, host='10.0.0.2'), NOW)  # private: never
    assert not geo.location_due(BoxState(HK_B), NOW)  # no address yet
    found = BoxState(HK, host='8.8.8.8', location={'country': 'US', 'at': NOW - 3600})
    assert not geo.location_due(found, NOW) and geo.location_due(found, NOW - 3600 + cfg.GEO_REFRESH_S)
    missed = BoxState(HK, host='8.8.8.8', location={'country': geo.UNKNOWN, 'at': NOW - 3600})
    assert not geo.location_due(missed, NOW) and geo.location_due(missed, NOW - 3600 + cfg.GEO_RETRY_S)


def test_the_pass_looks_up_the_boxes_that_are_due_oldest_first_and_at_most_a_few():
    answers = {'8.8.8.8': {'country': 'US', 'region': 'Texas', 'city': 'Dallas'}, '9.9.9.9': None}
    asked = []

    def lookup(ip):
        asked.append(ip)
        return answers.get(ip)

    boxes = {
        'a': BoxState('a', host='8.8.8.8'),
        'b': BoxState('b', host='9.9.9.9', location={'country': 'DE', 'at': NOW - 10 * 86_400}),  # a week old
        'c': BoxState('c', host='1.1.1.1', location={'country': 'DE', 'at': NOW - 60}),  # fresh
        'd': BoxState('d', host='10.0.0.4'),  # private
    }
    found = geo.refresh_locations(boxes, NOW, lookup, per_pass=1)
    assert asked == ['8.8.8.8'] and list(found) == ['a']  # never looked up comes before a stale one
    assert found['a'] == {'at': NOW, 'ip': '8.8.8.8', 'country': 'US', 'region': 'Texas', 'city': 'Dallas'}
    boxes['a'].location = found['a']
    found = geo.refresh_locations(boxes, NOW, lookup, per_pass=5)
    assert asked == ['8.8.8.8', '9.9.9.9'] and list(found) == ['b']
    assert found['b'] == {'at': NOW, 'ip': '9.9.9.9', 'country': geo.UNKNOWN, 'region': '', 'city': ''}  # a miss
    boxes['b'].location = found['b']
    assert geo.refresh_locations(boxes, NOW + 60, lookup, per_pass=5) == {}  # nothing due until the retry
    assert cfg.GEO_PER_PASS * 60 / cfg.DISCOVER_INTERVAL_S * 60 <= 45  # under ip-api's 45 lookups a minute


def test_an_order_that_names_a_country_is_placed_only_on_a_box_there(tmp_path):
    boxes = StateStore(tmp_path / 'boxes.json')
    store = rt.RentalStore(tmp_path / 'rentals.json')
    rec = rt.RentalReconciler(boxes, store, lambda box: None, wall=lambda: NOW)  # type: ignore[arg-type]
    us = rentable_box(location={'country': 'US', 'region': 'Texas', 'city': 'Dallas', 'at': NOW})
    boxes.put(us)
    boxes.put(rentable_box(hk=HK_B, uid=46))  # location unknown: not "there" for any country
    anywhere = rt.place_order(store, gpu_type='RTX5090', gpu_count=2, image='ubuntu:24.04', ssh_pubkeys=['k'], hours=1, now=NOW)  # fmt: skip
    assert rec._pick(anywhere, set(), NOW) is not None
    germany = rt.place_order(store, gpu_type='RTX5090', gpu_count=2, image='ubuntu:24.04', ssh_pubkeys=['k'], hours=1, now=NOW, country='de')  # fmt: skip
    assert germany.country == 'DE' and rec._pick(germany, set(), NOW) is None
    states = rt.place_order(store, gpu_type='RTX5090', gpu_count=2, image='ubuntu:24.04', ssh_pubkeys=['k'], hours=1, now=NOW, country='US')  # fmt: skip
    picked = rec._pick(states, set(), NOW)
    assert picked is not None and picked.box_id == HK
    # the app's order carries it over the seam, and the record round-trips
    app = FakeApp([order(country='us')])
    seam_store, p = poller(tmp_path / 'seam', app)
    p.take_orders()
    assert seam_store.rentals['rnt_app1'].country == 'US'
    assert rt.RentalRecord.from_dict(dataclasses.asdict(germany)).country == 'DE'  # round-trips through the store
