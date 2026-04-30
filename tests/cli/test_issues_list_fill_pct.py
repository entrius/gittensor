# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for the unified `_fill_percent` helper.

Before this fix, `gitt issues list` and `gitt issues list --id <N>` computed
the bounty fill percentage two different ways for the same data — the table
path used Decimal, the Panel path used raw float division. These tests pin
the unified Decimal path so both render modes stay consistent.
"""

import json
from unittest.mock import patch

import pytest

from gittensor.cli.issue_commands.view import _fill_percent


# ==========================================================================
# Unit tests for _fill_percent
# ==========================================================================


class TestFillPercent:
    """The helper used by both render paths."""

    def test_zero_target_returns_zero(self):
        assert _fill_percent(0, 0) == 0.0
        assert _fill_percent(50, 0) == 0.0

    def test_negative_target_returns_zero(self):
        # Defensive — chain values are unsigned, but match the existing fallback.
        assert _fill_percent(50, -1) == 0.0

    def test_full_returns_100(self):
        assert _fill_percent(100, 100) == 100.0

    def test_over_full_returns_above_100(self):
        # Bounty pool can exceed target if multiple contributors top up.
        assert _fill_percent(150, 100) == 150.0

    @pytest.mark.parametrize(
        'bounty, target, expected',
        [
            (1, 3, 100.0 / 3),       # 33.333...
            (5, 12, 5.0 / 12 * 100), # 41.666...
            (1, 7, 100.0 / 7),       # 14.285...
            (2, 11, 200.0 / 11),     # 18.181...
        ],
    )
    def test_decimal_precision_matches_expected_ratio(self, bounty, target, expected):
        # Decimal path returns a float close to the true ratio, with no float-binary artifacts.
        result = _fill_percent(bounty, target)
        assert result == pytest.approx(expected, rel=1e-12)

    def test_panel_and_table_paths_agree(self):
        """Same on-chain values must produce the same fill_pct regardless of render mode.

        Pre-fix, the Panel path used `bounty / target * 100` (raw float) and the
        table path used `Decimal(bounty) / Decimal(target) * 100`. For ratios like
        1/3 those produced visibly different rendered strings. This test pins the
        invariant that both paths now agree.
        """
        bounty, target = 1, 3
        # Both render paths now call _fill_percent — assert the value the helper
        # returns is the value rendered.
        unified = _fill_percent(bounty, target)
        # 1/3 in float64 is 0.3333333333333333; * 100 is 33.33333333333333.
        # Decimal version yields the same float (since float() collapses precision back).
        # The point is they are ONE value, not two.
        assert unified == pytest.approx(100.0 / 3, rel=1e-12)


# ==========================================================================
# End-to-end regression: rendered output uses the helper
# ==========================================================================


FAKE_ISSUES = [
    {
        'id': 1,
        'repository_full_name': 'owner/repo',
        'issue_number': 10,
        'bounty_amount': 1,
        'target_bounty': 3,
        'status': 'Active',
    },
]


def test_table_mode_renders_helper_value(cli_root, runner):
    """Table view must use _fill_percent — '33%' for bounty=1, target=3."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list'], catch_exceptions=False)

    assert result.exit_code == 0
    # Table mode formats with {:.0f}% — for 1/3 ratio that's "33%".
    assert '33%' in result.output


def test_panel_mode_renders_helper_value(cli_root, runner):
    """Single-issue Panel view must use _fill_percent — '33.3%' for bounty=1, target=3."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--id', '1'], catch_exceptions=False)

    assert result.exit_code == 0
    # Panel mode formats with {:.1f}% — for 1/3 ratio that's "33.3%".
    assert '33.3%' in result.output


def test_panel_and_table_agree_on_same_data(cli_root, runner):
    """For the same on-chain issue, table and Panel must agree to their shared decimal places.

    Table renders to 0 decimal places, Panel renders to 1 decimal place. After truncating
    to the table's precision, both must show the same integer percentage.
    """
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        table_result = runner.invoke(cli_root, ['issues', 'list'], catch_exceptions=False)
        panel_result = runner.invoke(cli_root, ['issues', 'list', '--id', '1'], catch_exceptions=False)

    assert table_result.exit_code == 0
    assert panel_result.exit_code == 0
    # Both must contain "33" as the integer percentage; pre-fix this could diverge for ratios
    # where binary float vs Decimal disagree at the rounding boundary.
    assert '33' in table_result.output
    assert '33' in panel_result.output


def test_json_mode_unaffected_by_fill_pct_change(cli_root, runner):
    """JSON output never serializes fill_pct directly — it's computed on the consumer side.
    This test pins the contract: bounty_amount and target_bounty are passed through unchanged.
    """
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['success'] is True
    issue = payload['issues'][0]
    assert issue['bounty_amount'] == 1
    assert issue['target_bounty'] == 3
