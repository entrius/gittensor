"""The per-miner UID log filter keeps concurrent scoring lines attributable."""

import logging

from gittensor.utils.logging import (
    _scoring_uid,
    _UidLogFilter,
    install_uid_log_filter,
    scoring_uid,
)


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord('bittensor', logging.INFO, 'f.py', 1, msg, None, None)


def test_filter_tags_message_when_uid_is_set():
    flt = _UidLogFilter()
    record = _record('scoring 3 merged')
    with scoring_uid(42):
        flt.filter(record)
    assert record.msg == '[UID 42] scoring 3 merged'


def test_filter_leaves_message_untouched_without_uid():
    flt = _UidLogFilter()
    record = _record('startup line')
    flt.filter(record)
    assert record.msg == 'startup line'


def test_filter_tags_each_record_once():
    flt = _UidLogFilter()
    record = _record('once')
    with scoring_uid(1):
        flt.filter(record)
        flt.filter(record)  # a second pass must not double-prefix
    assert record.msg == '[UID 1] once'


def test_scoring_uid_resets_on_exit():
    assert _scoring_uid.get() is None
    with scoring_uid(9):
        assert _scoring_uid.get() == 9
    assert _scoring_uid.get() is None


def test_install_uid_log_filter_is_idempotent():
    install_uid_log_filter()
    install_uid_log_filter()
    bittensor_logger = logging.getLogger('bittensor')
    assert sum(isinstance(f, _UidLogFilter) for f in bittensor_logger.filters) == 1
