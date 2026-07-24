# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for the destructive-operation confirmation gate.

``confirm_or_abort`` guards ownership transfer, bounty payouts, and validator
whitelist edits. A non-interactive stdin must fail closed rather than
self-confirm, so an unattended run can never perform an irreversible action
that the operator never approved.
"""

from unittest.mock import patch

import pytest

from gittensor.cli.issue_commands.helpers import confirm_or_abort


class TestConfirmOrAbortInteractive:
    """With a TTY the prompt decides."""

    def test_accepts_when_user_confirms(self):
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True),
            patch('gittensor.cli.issue_commands.helpers.click.confirm', return_value=True),
        ):
            assert confirm_or_abort('Transfer ownership?', yes=False) is True

    def test_returns_false_when_user_declines(self):
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True),
            patch('gittensor.cli.issue_commands.helpers.click.confirm', return_value=False),
        ):
            assert confirm_or_abort('Transfer ownership?', yes=False) is False

    def test_default_is_forwarded_to_prompt(self):
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True),
            patch('gittensor.cli.issue_commands.helpers.click.confirm', return_value=True) as confirm,
        ):
            confirm_or_abort('Proceed with registration?', yes=False, default=True)
        assert confirm.call_args.kwargs['default'] is True


class TestConfirmOrAbortYesFlag:
    """--yes is the only supported way to skip the prompt."""

    def test_yes_skips_prompt_on_a_tty(self):
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True),
            patch('gittensor.cli.issue_commands.helpers.click.confirm') as confirm,
        ):
            assert confirm_or_abort('Pay out issue 1?', yes=True) is True
        confirm.assert_not_called()

    def test_yes_skips_prompt_without_a_tty(self):
        """Scripts and CI opt in explicitly and keep working."""
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=False),
            patch('gittensor.cli.issue_commands.helpers.click.confirm') as confirm,
        ):
            assert confirm_or_abort('Pay out issue 1?', yes=True) is True
        confirm.assert_not_called()


class TestConfirmOrAbortNonInteractiveFailsClosed:
    """Without --yes, a non-TTY stdin aborts instead of silently proceeding."""

    def test_exits_non_zero_instead_of_proceeding(self):
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=False),
            patch('gittensor.cli.issue_commands.helpers.click.confirm') as confirm,
            pytest.raises(SystemExit) as exc,
        ):
            confirm_or_abort('Transfer ownership to 5Hxxx?', yes=False)

        assert exc.value.code == 1
        # Never fell through to a prompt that cannot be answered.
        confirm.assert_not_called()

    def test_error_names_the_action_and_the_opt_in_flag(self, capsys):
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=False),
            pytest.raises(SystemExit),
        ):
            confirm_or_abort('Transfer ownership to 5Hxxx?', yes=False)

        output = capsys.readouterr().err
        assert 'Transfer ownership to 5Hxxx?' in output
        assert '--yes' in output

    def test_default_true_does_not_auto_confirm_without_a_tty(self):
        """A permissive prompt default must not become an implicit approval."""
        with (
            patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=False),
            pytest.raises(SystemExit),
        ):
            confirm_or_abort('Proceed with registration?', yes=False, default=True)
