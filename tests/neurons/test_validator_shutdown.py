"""Regression tests for BaseValidatorNeuron shutdown lifecycle.

Covers three correctness bugs in the shutdown path:
  1. Ctrl-C must not crash with `--neuron.axon_off True`.
  2. SIGTERM must trigger the same cleanup path as KeyboardInterrupt.
  3. stop_run_thread must warn when the thread fails to exit within the join timeout.
"""

import logging
import signal
import threading
import time
from unittest.mock import MagicMock

import pytest

from neurons.base.validator import BaseValidatorNeuron


class _ConcreteValidator(BaseValidatorNeuron):
    """Concrete subclass used only for tests — BaseValidatorNeuron is abstract."""

    # Override `block` (a property in the parent) with a plain class attribute
    # so tests can set it without the real subtensor RPC call.
    block = 0  # type: ignore[assignment]

    async def forward(self):  # type: ignore[override]
        return None


def _make_validator(axon_off: bool, raise_in_loop: BaseException = KeyboardInterrupt()):
    """Build a minimal validator instance without invoking real init.

    Bypasses __init__ so we don't pull in bittensor wallet/subtensor setup.
    """
    v = _ConcreteValidator.__new__(_ConcreteValidator)
    v.config = MagicMock()
    v.config.neuron.axon_off = axon_off
    v.config.subtensor.chain_endpoint = 'mock'
    v.config.netuid = 422
    v.step = 0
    v.should_exit = False
    v.loop = MagicMock()
    v.loop.run_until_complete.side_effect = raise_in_loop
    v.sync = MagicMock()
    v.concurrent_forward = MagicMock()
    if not axon_off:
        v.axon = MagicMock()
    return v


# ---------------------------------------------------------------------------
# Bug 1: axon_off + Ctrl-C
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_with_axon_off_does_not_attribute_error():
    """Ctrl-C with axon_off=True must shut down cleanly, not crash with AttributeError."""
    v = _make_validator(axon_off=True)
    # sys.exit raises SystemExit; that's the expected clean termination.
    with pytest.raises(SystemExit) as exc_info:
        v.run()
    assert exc_info.value.code == 0


def test_keyboard_interrupt_with_axon_on_calls_axon_stop():
    """Ctrl-C with axon serving must stop the axon before exit."""
    v = _make_validator(axon_off=False)
    with pytest.raises(SystemExit):
        v.run()
    v.axon.stop.assert_called_once()


def test_keyboard_interrupt_with_axon_off_does_not_call_anything_on_missing_axon():
    """axon_off=True means self.axon was never created; the guard must skip the call."""
    v = _make_validator(axon_off=True)
    assert not hasattr(v, 'axon')
    with pytest.raises(SystemExit):
        v.run()
    # If we get here without AttributeError, the guard worked.
    assert not hasattr(v, 'axon')


# ---------------------------------------------------------------------------
# Bug 2: SIGTERM handler
# ---------------------------------------------------------------------------


def test_handle_sigterm_raises_keyboard_interrupt():
    """The SIGTERM handler must convert the signal to KeyboardInterrupt so it
    flows through the same cleanup path as Ctrl-C."""
    with pytest.raises(KeyboardInterrupt):
        BaseValidatorNeuron._handle_sigterm(signal.SIGTERM, None)


def test_run_under_simulated_sigterm_invokes_axon_stop():
    """End-to-end: simulating SIGTERM during run() must end up calling axon.stop()."""
    v = _make_validator(axon_off=False, raise_in_loop=KeyboardInterrupt('SIGTERM received'))
    with pytest.raises(SystemExit):
        v.run()
    v.axon.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Bug 3: silent thread.join timeout
# ---------------------------------------------------------------------------


def test_stop_run_thread_warns_when_thread_does_not_exit(caplog):
    """If the validator thread is still alive after join(5), stop_run_thread
    must emit a warning instead of silently flipping is_running to False."""
    v = _ConcreteValidator.__new__(_ConcreteValidator)
    v.is_running = True

    # Build a thread that will refuse to exit before join(5) returns.
    stop_event = threading.Event()

    def _runner():
        # Block until externally released; join(5) will time out before this.
        stop_event.wait(timeout=30)

    real_thread = threading.Thread(target=_runner, daemon=True)
    real_thread.start()
    v.thread = real_thread
    v.should_exit = False  # the runner ignores it; we just need is_alive() True

    try:
        with caplog.at_level(logging.WARNING):
            v.stop_run_thread()
        # The thread is still alive — assert the warning fired.
        warnings = [r.message for r in caplog.records if 'did not exit' in r.message.lower()]
        assert warnings, f'Expected timeout warning, got records: {[r.message for r in caplog.records]}'
        assert v.is_running is False  # flag is still flipped, but the warning surfaces the truth
    finally:
        stop_event.set()
        real_thread.join(timeout=5)


def test_stop_run_thread_does_not_warn_when_thread_exits_cleanly(caplog):
    """Happy path: thread exits before join timeout, no warning emitted."""
    v = _ConcreteValidator.__new__(_ConcreteValidator)
    v.is_running = True

    def _runner():
        time.sleep(0.05)  # exits well before join(5)

    real_thread = threading.Thread(target=_runner, daemon=True)
    real_thread.start()
    v.thread = real_thread
    v.should_exit = False

    with caplog.at_level(logging.WARNING):
        v.stop_run_thread()

    warnings = [r.message for r in caplog.records if 'did not exit' in r.message.lower()]
    assert not warnings, f'Did not expect warning, got: {warnings}'
    assert v.is_running is False
