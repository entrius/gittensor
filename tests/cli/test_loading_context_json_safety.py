# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests: --json-accepting commands must use loading_context, not console.status.

The helper `loading_context(msg, as_json)` short-circuits to nullcontext when
as_json=True, so the Rich spinner never runs in JSON mode. Bypassing the helper
risks (a) spinner Unicode bytes polluting stdout for TTY operators, and
(b) UnicodeEncodeError crashes on non-UTF-8 stdout (latin-1, Windows-1252).
"""

from contextlib import nullcontext
from unittest.mock import patch

from gittensor.cli.issue_commands.helpers import loading_context


# ==========================================================================
# Unit tests on loading_context itself
# ==========================================================================


class TestLoadingContextHelper:
    """The helper that JSON-accepting commands must use instead of console.status."""

    def test_json_mode_returns_nullcontext(self):
        """as_json=True must short-circuit to a no-op context — no spinner, no stdout writes."""
        ctx = loading_context('reading...', as_json=True)
        # nullcontext is the only valid no-op context manager from the stdlib;
        # implementation detail isn't strictly pinned, but the type is.
        assert isinstance(ctx, type(nullcontext()))

    def test_human_mode_returns_status(self):
        """as_json=False must return a Rich status (not nullcontext)."""
        ctx = loading_context('reading...', as_json=False)
        assert not isinstance(ctx, type(nullcontext()))

    def test_json_mode_is_silent(self, capsys):
        """Entering and exiting the JSON-mode context must produce zero stdout/stderr."""
        with loading_context('should be silent', as_json=True):
            pass
        captured = capsys.readouterr()
        assert captured.out == ''
        assert captured.err == ''


# ==========================================================================
# Integration: --json commands actually call loading_context (not console.status)
# ==========================================================================


FAKE_ISSUES = [
    {
        'id': 1,
        'repository_full_name': 'owner/repo',
        'issue_number': 10,
        'bounty_amount': 50,
        'target_bounty': 100,
        'status': 'Active',
    },
]


def test_issues_list_json_uses_loading_context(cli_root, runner):
    """`gitt issues list --json` must funnel through loading_context — captured by mock."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
        patch('gittensor.cli.issue_commands.view.loading_context', wraps=loading_context) as mock_ctx,
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code == 0
    # The command must call loading_context (not console.status directly).
    assert mock_ctx.called
    # And it must pass as_json=True so the helper short-circuits to nullcontext.
    args, kwargs = mock_ctx.call_args
    # Helper signature: loading_context(message, as_json, ...)
    as_json_arg = kwargs.get('as_json', args[1] if len(args) >= 2 else None)
    assert as_json_arg is True


def test_issues_list_human_mode_passes_as_json_false(cli_root, runner):
    """In human mode, loading_context must be called with as_json=False so the spinner runs."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
        patch('gittensor.cli.issue_commands.view.loading_context', wraps=loading_context) as mock_ctx,
    ):
        result = runner.invoke(cli_root, ['issues', 'list'], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_ctx.called
    args, kwargs = mock_ctx.call_args
    as_json_arg = kwargs.get('as_json', args[1] if len(args) >= 2 else None)
    assert as_json_arg is False
