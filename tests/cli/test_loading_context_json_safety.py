"""Tests for loading_context JSON safety — spinner must not run in --json mode."""

import io
import sys

import pytest

from gittensor.cli.issue_commands.helpers import loading_context


def test_loading_context_json_mode_is_noop():
    """loading_context with as_json=True must be a no-op context manager."""
    with loading_context('Test message', as_json=True):
        pass  # must not raise


def test_loading_context_json_mode_no_stdout():
    """loading_context with as_json=True must not write anything to stdout."""
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        with loading_context('Test message', as_json=True):
            pass
    finally:
        sys.stdout = old_stdout
    assert buf.getvalue() == ''


def test_loading_context_json_mode_no_stderr():
    """loading_context with as_json=True must not write anything to stderr."""
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        with loading_context('Test message', as_json=True):
            pass
    finally:
        sys.stderr = old_stderr
    assert buf.getvalue() == ''


def test_loading_context_human_mode_does_not_raise():
    """loading_context with as_json=False must not raise."""
    try:
        with loading_context('Test message', as_json=False):
            pass
    except Exception as e:
        pytest.fail(f'loading_context raised in human mode: {e}')


def test_loading_context_json_mode_body_executes():
    """loading_context with as_json=True must still execute the body."""
    executed = []
    with loading_context('Test message', as_json=True):
        executed.append(True)
    assert executed == [True]
