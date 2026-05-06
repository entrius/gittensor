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

    def test_panel_and_table_paths_agree_at_rounding_boundary(self):
        """At a rounding-boundary ratio, the helper produces the Decimal-correct value.

        Pre-fix, the Panel path used `bounty / target * 100` (raw float). For
        bounty=23, target=80 that yields 28.749999999999996 (binary-float artifact),
        which renders as "28.7%" at :.1f. The Decimal helper yields exactly 28.75,
        which renders as "28.8%". This pins the helper to the Decimal-correct value
        so the Panel path no longer drifts off by a tenth at rounding boundaries.
        """
        bounty, target = 23, 80
        unified = _fill_percent(bounty, target)
        # Helper returns the exact Decimal value: 28.75, not the binary-float 28.749999...
        assert unified == 28.75
        # The pre-fix Panel formula would have given 28.749999999999996 here.
        assert (bounty / target * 100) != unified


# ==========================================================================
# End-to-end regression: rendered output uses the helper
# ==========================================================================


FAKE_ISSUES = [
    {
        'id': 1,
        'repository_full_name': 'owner/repo',
        'issue_number': 10,
        # 23/80 is a rounding-boundary case: the pre-fix Panel formula
        # `bounty / target * 100` produces 28.749999999999996 (binary-float artifact)
        # which renders as "28.7%" at :.1f, while the Decimal helper produces
        # exactly 28.75 which renders as "28.8%". This makes the Panel-mode
        # assertion below fail against the pre-fix code, demonstrating the bug.
        'bounty_amount': 23,
        'target_bounty': 80,
        'status': 'Active',
    },
]


def test_table_mode_renders_helper_value(cli_root, runner):
    """Table view uses _fill_percent — '29%' for bounty=23, target=80 (28.75 → :.0f)."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list'], catch_exceptions=False)

    assert result.exit_code == 0
    # Table mode formats with {:.0f}% — 28.75 rounds to "29".
    assert '29%' in result.output


def test_panel_mode_renders_helper_value(cli_root, runner):
    """Single-issue Panel view must use _fill_percent — '28.8%' for bounty=23, target=80.

    This is the load-bearing regression assertion. Pre-fix, the Panel path computed
    `23 / 80 * 100 = 28.749999999999996` (binary-float artifact) which renders as
    "28.7%" at :.1f. The Decimal helper computes 28.75 exactly, which renders as
    "28.8%". Asserting "28.8%" present AND "28.7%" absent fails against the
    pre-fix code and passes against the unified helper.
    """
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--id', '1'], catch_exceptions=False)

    assert result.exit_code == 0
    assert '28.8%' in result.output
    assert '28.7%' not in result.output


def test_panel_and_table_agree_on_same_data(cli_root, runner):
    """Table and Panel must render the same on-chain value coherently.

    Table renders 28.75 at :.0f as "29"; Panel renders 28.75 at :.1f as "28.8".
    Pre-fix, the Panel path's binary-float artifact rendered "28.7" — under the
    unified helper, both views agree at the table's rounded integer "29".
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
    assert '29%' in table_result.output
    assert '28.8%' in panel_result.output


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
    assert issue['bounty_amount'] == 23
    assert issue['target_bounty'] == 80
