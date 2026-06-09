# Entrius 2025

"""Tests for `gitt miner scan`."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.miner_commands.scan import (
    Opportunity,
    _best_label_bonus,
    _check_competing_prs,
    _fetch_open_issues,
    _freshness_factor,
    _parse_dt,
    _potential_issue_multiplier,
    _score_issue,
)


@pytest.fixture
def runner():
    return CliRunner()


def _repo_cfg(emission_share=0.09, label_multipliers=None, default_label_multiplier=1.0):
    from gittensor.validator.utils.load_weights import RepositoryConfig

    return RepositoryConfig(
        emission_share=emission_share,
        issue_discovery_share=0.0,
        label_multipliers=label_multipliers,
        default_label_multiplier=default_label_multiplier,
    )


def _raw_issue(number=1, title="Fix the bug", author_association="NONE", labels=None, created_at="2026-05-01T00:00:00Z", comments=0):
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/test/repo/issues/{number}",
        "author_association": author_association,
        "labels": [{"name": l} for l in (labels or [])],
        "created_at": created_at,
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Unit: _parse_dt
# ---------------------------------------------------------------------------

class TestParseDt:
    def test_z_suffix(self):
        dt = _parse_dt("2026-05-01T12:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_none_returns_none(self):
        assert _parse_dt(None) is None

    def test_invalid_returns_none(self):
        assert _parse_dt("not-a-date") is None


# ---------------------------------------------------------------------------
# Unit: _freshness_factor
# ---------------------------------------------------------------------------

class TestFreshnessFactor:
    def test_very_fresh_issue_scores_one(self):
        dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        # 0 days old
        assert _freshness_factor(dt) == 1.0

    def test_14_days_old_is_full(self):
        from datetime import timedelta
        dt = datetime.now(timezone.utc) - timedelta(days=13)
        assert _freshness_factor(dt) == 1.0

    def test_60_days_old_hits_floor(self):
        from datetime import timedelta
        dt = datetime.now(timezone.utc) - timedelta(days=60)
        assert _freshness_factor(dt) == pytest.approx(0.3, abs=0.05)

    def test_30_days_old_is_roughly_half(self):
        from datetime import timedelta
        dt = datetime.now(timezone.utc) - timedelta(days=30)
        f = _freshness_factor(dt)
        assert 0.3 <= f <= 0.7


# ---------------------------------------------------------------------------
# Unit: _best_label_bonus
# ---------------------------------------------------------------------------

class TestBestLabelBonus:
    def test_matching_label_returns_multiplier(self):
        cfg = _repo_cfg(label_multipliers={"feature": 1.5, "bug": 1.1})
        bonus, name = _best_label_bonus(["feature"], cfg)
        assert bonus == pytest.approx(1.5)
        assert name == "feature"

    def test_no_label_multipliers_returns_one(self):
        cfg = _repo_cfg()
        bonus, name = _best_label_bonus(["feature"], cfg)
        assert bonus == 1.0
        assert name == ""

    def test_no_matching_label_returns_one(self):
        cfg = _repo_cfg(label_multipliers={"feature": 1.5})
        bonus, name = _best_label_bonus(["bug"], cfg)
        assert bonus == 1.0
        assert name == ""

    def test_picks_best_when_multiple(self):
        cfg = _repo_cfg(label_multipliers={"feature": 1.5, "bug": 1.1})
        bonus, name = _best_label_bonus(["bug", "feature"], cfg)
        assert bonus == pytest.approx(1.5)
        assert name == "feature"


# ---------------------------------------------------------------------------
# Unit: _potential_issue_multiplier
# ---------------------------------------------------------------------------

class TestPotentialIssueMultiplier:
    def test_none_association_gives_standard(self):
        cfg = _repo_cfg()
        mult = _potential_issue_multiplier("NONE", cfg)
        assert mult == pytest.approx(1.33)

    def test_owner_gives_maintainer(self):
        cfg = _repo_cfg()
        mult = _potential_issue_multiplier("OWNER", cfg)
        assert mult == pytest.approx(1.66)

    def test_member_gives_maintainer(self):
        cfg = _repo_cfg()
        mult = _potential_issue_multiplier("MEMBER", cfg)
        assert mult == pytest.approx(1.66)


# ---------------------------------------------------------------------------
# Unit: _score_issue
# ---------------------------------------------------------------------------

class TestScoreIssue:
    def test_basic_scoring(self):
        cfg = _repo_cfg(emission_share=0.09)
        raw = _raw_issue(created_at="2026-06-01T00:00:00Z")
        session = MagicMock()
        opp = _score_issue("test/repo", raw, cfg, check_prs=False, session=session)
        assert opp is not None
        assert opp.repo == "test/repo"
        assert opp.issue_number == 1
        assert opp.opportunity_score > 0
        # score = 0.09 * 1.33 * 1.0 * 1.0 * freshness
        assert opp.opportunity_score <= 0.09 * 1.33 * 1.0 + 1e-6

    def test_default_label_multiplier_zero_skips_unlabelled(self):
        cfg = _repo_cfg(default_label_multiplier=0.0, label_multipliers={"feature": 1.0})
        raw = _raw_issue(labels=[])  # no labels
        session = MagicMock()
        opp = _score_issue("test/repo", raw, cfg, check_prs=False, session=session)
        assert opp is None

    def test_default_label_multiplier_zero_keeps_labelled(self):
        cfg = _repo_cfg(default_label_multiplier=0.0, label_multipliers={"feature": 1.0})
        raw = _raw_issue(labels=["feature"])
        session = MagicMock()
        opp = _score_issue("test/repo", raw, cfg, check_prs=False, session=session)
        assert opp is not None

    def test_competing_pr_reduces_score(self):
        cfg = _repo_cfg(emission_share=0.09)
        raw = _raw_issue(created_at="2026-06-01T00:00:00Z")
        session = MagicMock()

        # Without competing PR
        opp_free = _score_issue("test/repo", raw, cfg, check_prs=False, session=session)

        # With competing PR injected via mock
        with patch("gittensor.cli.miner_commands.scan._check_competing_prs", return_value=True):
            opp_compete = _score_issue("test/repo", raw, cfg, check_prs=True, session=session)

        assert opp_compete is not None
        assert opp_free is not None
        assert opp_compete.opportunity_score < opp_free.opportunity_score

    def test_maintainer_issue_scores_higher(self):
        cfg = _repo_cfg(emission_share=0.09)
        raw_standard = _raw_issue(author_association="NONE", created_at="2026-06-01T00:00:00Z")
        raw_maintainer = _raw_issue(author_association="OWNER", created_at="2026-06-01T00:00:00Z")
        session = MagicMock()
        opp_std = _score_issue("t/r", raw_standard, cfg, check_prs=False, session=session)
        opp_mnt = _score_issue("t/r", raw_maintainer, cfg, check_prs=False, session=session)
        assert opp_mnt is not None and opp_std is not None
        assert opp_mnt.opportunity_score > opp_std.opportunity_score


# ---------------------------------------------------------------------------
# Unit: _fetch_open_issues
# ---------------------------------------------------------------------------

class TestFetchOpenIssues:
    def test_filters_pull_requests(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"number": 1, "title": "Issue", "pull_request": {}},  # PR — should be filtered
            {"number": 2, "title": "Real issue"},                  # real issue
        ]
        session = MagicMock()
        session.get.return_value = mock_resp
        issues = _fetch_open_issues(session, "test/repo")
        assert len(issues) == 1
        assert issues[0]["number"] == 2

    def test_404_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        session = MagicMock()
        session.get.return_value = mock_resp
        assert _fetch_open_issues(session, "nonexistent/repo") == []


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestScanCli:
    def test_missing_pat_exits_nonzero(self, runner: CliRunner):
        result = runner.invoke(cli, ["miner", "scan"])
        assert result.exit_code != 0

    def test_scan_shows_table(self, runner: CliRunner):
        from gittensor.validator.utils.load_weights import RepositoryConfig

        repos = {
            "entrius/gittensor": RepositoryConfig(
                emission_share=0.09,
                issue_discovery_share=0.0,
                label_multipliers={"feature": 1.5},
            )
        }
        issues = [
            {
                "number": 42,
                "title": "Add dark mode support",
                "html_url": "https://github.com/entrius/gittensor/issues/42",
                "author_association": "NONE",
                "labels": [{"name": "feature"}],
                "created_at": "2026-06-01T00:00:00Z",
                "comments": 3,
            }
        ]

        with (
            patch("gittensor.validator.utils.load_weights.load_master_repo_weights", return_value=repos),
            patch("gittensor.cli.miner_commands.scan._fetch_open_issues", return_value=issues),
        ):
            result = runner.invoke(cli, ["miner", "scan", "--pat", "ghp_test"])

        assert result.exit_code == 0, result.output
        assert "entrius/gittensor" in result.output
        assert "feature" in result.output
        assert "Issue Opportunities" in result.output

    def test_scan_no_issues_shows_message(self, runner: CliRunner):
        from gittensor.validator.utils.load_weights import RepositoryConfig

        repos = {"test/repo": RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0)}

        with (
            patch("gittensor.validator.utils.load_weights.load_master_repo_weights", return_value=repos),
            patch("gittensor.cli.miner_commands.scan._fetch_open_issues", return_value=[]),
        ):
            result = runner.invoke(cli, ["miner", "scan", "--pat", "ghp_test"])

        assert result.exit_code == 0
        assert "No open issues" in result.output
