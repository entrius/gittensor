# Entrius 2025

"""Tests for `gitt miner advisor`."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.miner_commands.advisor import Severity, _analyse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


def _make_eval(
    uid: int = 1,
    hotkey: str = "dev",
    github_id: str = "42",
    failed_reason=None,
    **overrides,
):
    from gittensor.classes import MinerEvaluation

    ev = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id, failed_reason=failed_reason)
    for k, v in overrides.items():
        setattr(ev, k, v)
    return ev


def _minimal_repo_configs() -> Dict:
    """One high-value repo with no per-repo overrides."""
    from gittensor.validator.utils.load_weights import RepositoryConfig

    return {
        "entrius/gittensor": RepositoryConfig(
            emission_share=0.09,
            issue_discovery_share=0.0,
            label_multipliers={"feature": 1.5, "bug": 1.1},
            trusted_label_pipeline=True,
        )
    }


# ---------------------------------------------------------------------------
# Unit tests: _analyse
# ---------------------------------------------------------------------------


class TestAnalyse:
    def test_failed_identity_returns_critical(self):
        ev = _make_eval(failed_reason="PAT invalid")
        advice = _analyse(ev, _minimal_repo_configs(), {})
        assert any(a.severity == Severity.CRITICAL for a in advice)
        assert any("PAT" in a.message or "failed" in a.message.lower() for a in advice)

    def test_no_merged_prs_returns_critical(self):
        ev = _make_eval()
        advice = _analyse(ev, _minimal_repo_configs(), {})
        criticals = [a for a in advice if a.severity == Severity.CRITICAL]
        assert criticals, "Expected CRITICAL advice when miner has 0 merged PRs"
        assert any("entrius/gittensor" in (a.repo or "") for a in criticals)

    def test_eligible_miner_no_issues_linked_returns_tip(self):
        """A miner who is eligible but has no issue-linked PRs should get a TIP."""
        from gittensor.classes import MinerEvaluation, RepoEvaluation
        from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
        from gittensor.utils.mirror.models import MirrorPullRequest

        ev = _make_eval()

        # Build 3 merged ScoredPRs with issue_multiplier=1.0 (no linked issue)
        for i in range(1, 4):
            pr = MagicMock(spec=MirrorPullRequest)
            pr.pr_number = i
            pr.repo_full_name = "entrius/gittensor"
            pr.state = "MERGED"
            pr.merged_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
            pr.author_association = "CONTRIBUTOR"

            spr = MagicMock(spec=ScoredPR)
            spr.pr = pr
            spr.issue_multiplier = 1.0
            spr.label = None
            spr.label_multiplier = 1.0
            spr.time_decay_multiplier = 0.95
            spr.review_quality_multiplier = 1.0
            spr.token_score = 50.0
            ev.merged_prs.append(spr)

        ev.unique_repos_contributed_to.add("entrius/gittensor")

        repo_eval = RepoEvaluation(
            repository_full_name="entrius/gittensor",
            total_merged_prs=3,
            total_open_prs=0,
            total_closed_prs=0,
        )
        repo_eval.is_eligible = True
        repo_eval.credibility = 1.0
        repo_eval.total_score = 45.0
        repo_eval.issue_discovery_score = 0.0
        ev.repo_evaluations["entrius/gittensor"] = repo_eval

        advice = _analyse(ev, _minimal_repo_configs(), {})
        tips = [a for a in advice if a.severity == Severity.TIP and a.repo == "entrius/gittensor"]
        assert tips, "Expected TIP about missing issue links"
        assert any("issue" in a.message.lower() for a in tips)

    def test_open_pr_spam_penalty_warning(self):
        """Exceeding the open-PR threshold should produce a WARNING."""
        from gittensor.classes import MinerEvaluation, RepoEvaluation
        from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
        from gittensor.utils.mirror.models import MirrorPullRequest

        ev = _make_eval()

        # 3 merged PRs to pass eligibility
        for i in range(1, 4):
            pr = MagicMock(spec=MirrorPullRequest)
            pr.pr_number = i
            pr.repo_full_name = "entrius/gittensor"
            pr.state = "MERGED"
            pr.merged_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
            spr = MagicMock(spec=ScoredPR)
            spr.pr = pr
            spr.issue_multiplier = 1.0
            spr.label = None
            spr.label_multiplier = 1.0
            spr.time_decay_multiplier = 1.0
            spr.review_quality_multiplier = 1.0
            spr.token_score = 10.0
            ev.merged_prs.append(spr)

        # 5 open PRs — exceeds default threshold of 2
        for i in range(10, 15):
            pr = MagicMock(spec=MirrorPullRequest)
            pr.pr_number = i
            pr.repo_full_name = "entrius/gittensor"
            pr.state = "OPEN"
            spr = MagicMock(spec=ScoredPR)
            spr.pr = pr
            ev.open_prs.append(spr)

        ev.unique_repos_contributed_to.add("entrius/gittensor")
        repo_eval = RepoEvaluation(
            repository_full_name="entrius/gittensor",
            total_merged_prs=3,
            total_open_prs=5,
            total_closed_prs=0,
        )
        repo_eval.is_eligible = True
        repo_eval.credibility = 1.0
        repo_eval.total_score = 0.0  # zeroed by spam penalty
        repo_eval.issue_discovery_score = 0.0
        ev.repo_evaluations["entrius/gittensor"] = repo_eval

        advice = _analyse(ev, _minimal_repo_configs(), {})
        warnings = [a for a in advice if a.severity == Severity.WARNING and a.repo == "entrius/gittensor"]
        assert warnings, "Expected WARNING about open PR spam"
        assert any("spam" in a.message.lower() or "open pr" in a.message.lower() for a in warnings)

    def test_credibility_below_threshold_returns_critical(self):
        """Credibility below min_credibility should block eligibility and produce CRITICAL."""
        from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
        from gittensor.utils.mirror.models import MirrorPullRequest

        ev = _make_eval()

        # 3 merged PRs + 10 closed PRs → credibility = 3/13 ≈ 0.23, below 0.80
        for i in range(1, 4):
            pr = MagicMock(spec=MirrorPullRequest)
            pr.pr_number = i
            pr.repo_full_name = "entrius/gittensor"
            pr.state = "MERGED"
            pr.merged_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
            spr = MagicMock(spec=ScoredPR)
            spr.pr = pr
            spr.issue_multiplier = 1.0
            spr.label = None
            spr.label_multiplier = 1.0
            spr.time_decay_multiplier = 1.0
            spr.review_quality_multiplier = 1.0
            spr.token_score = 10.0
            ev.merged_prs.append(spr)

        for i in range(100, 110):
            pr = MagicMock(spec=MirrorPullRequest)
            pr.pr_number = i
            pr.repo_full_name = "entrius/gittensor"
            pr.state = "CLOSED"
            spr = MagicMock(spec=ScoredPR)
            spr.pr = pr
            ev.closed_prs.append(spr)

        advice = _analyse(ev, _minimal_repo_configs(), {})
        criticals = [a for a in advice if a.severity == Severity.CRITICAL and a.repo == "entrius/gittensor"]
        assert criticals, "Expected CRITICAL for low credibility"
        assert any("credib" in a.message.lower() for a in criticals)

    def test_high_value_untouched_repo_returns_tip(self):
        """Repos with ≥4% emission_share the miner has never touched should generate a TIP."""
        from gittensor.validator.utils.load_weights import RepositoryConfig

        repos = {
            "entrius/gittensor": RepositoryConfig(emission_share=0.09, issue_discovery_share=0.0),
            "infiniflow/ragflow": RepositoryConfig(emission_share=0.055, issue_discovery_share=0.0),
        }
        ev = _make_eval()
        # miner contributed to only one repo
        ev.unique_repos_contributed_to.add("entrius/gittensor")

        advice = _analyse(ev, repos, {})
        tips = [a for a in advice if a.severity == Severity.TIP and a.repo is None]
        assert any("ragflow" in a.message for a in tips), (
            "Expected TIP mentioning the untouched high-value repo"
        )


# ---------------------------------------------------------------------------
# Integration-level CLI smoke test
# ---------------------------------------------------------------------------


class TestAdvisorCli:
    def _patch_pipeline(self, ev, blended: float = 0.0):
        repos = _minimal_repo_configs()
        miner_evaluations = {1: ev}
        final_rewards = np.array([blended])

        oss_mock = AsyncMock(return_value=(miner_evaluations, set(), set()))
        issue_mock = AsyncMock(return_value=None)
        blend_mock = MagicMock(return_value=final_rewards)
        build_mock = MagicMock(return_value={})
        load_repos_mock = MagicMock(return_value=repos)
        load_langs_mock = MagicMock(return_value={})
        load_token_mock = MagicMock(return_value=MagicMock(language_configs={}))

        return {
            "oss": oss_mock,
            "issue": issue_mock,
            "blend": blend_mock,
            "build": build_mock,
            "load_repos": load_repos_mock,
            "load_langs": load_langs_mock,
            "load_token": load_token_mock,
        }

    def test_missing_pat_exits_nonzero(self, runner: CliRunner):
        result = runner.invoke(cli, ["miner", "advisor"])
        assert result.exit_code != 0

    def test_failed_identity_shows_critical(self, runner: CliRunner):
        ev = _make_eval(failed_reason="PAT invalid or expired")
        mocks = self._patch_pipeline(ev)

        with (
            patch("gittensor.validator.forward.oss_contributions", mocks["oss"]),
            patch("gittensor.validator.forward.issue_discovery", mocks["issue"]),
            patch("gittensor.validator.emission_allocation.blend_emission_pools", mocks["blend"]),
            patch("gittensor.validator.forward.build_maintainer_uids_by_repo", mocks["build"]),
            patch(
                "gittensor.validator.utils.load_weights.load_master_repo_weights",
                mocks["load_repos"],
            ),
            patch(
                "gittensor.validator.utils.load_weights.load_programming_language_weights",
                mocks["load_langs"],
            ),
            patch(
                "gittensor.validator.utils.load_weights.load_token_config",
                mocks["load_token"],
            ),
        ):
            result = runner.invoke(cli, ["miner", "advisor", "--pat", "ghp_test"])

        assert result.exit_code == 0
        assert "CRITICAL" in result.output

    def test_eligible_miner_output_contains_panel(self, runner: CliRunner):
        ev = _make_eval()
        mocks = self._patch_pipeline(ev, blended=0.05)

        with (
            patch("gittensor.validator.forward.oss_contributions", mocks["oss"]),
            patch("gittensor.validator.forward.issue_discovery", mocks["issue"]),
            patch("gittensor.validator.emission_allocation.blend_emission_pools", mocks["blend"]),
            patch("gittensor.validator.forward.build_maintainer_uids_by_repo", mocks["build"]),
            patch(
                "gittensor.validator.utils.load_weights.load_master_repo_weights",
                mocks["load_repos"],
            ),
            patch(
                "gittensor.validator.utils.load_weights.load_programming_language_weights",
                mocks["load_langs"],
            ),
            patch(
                "gittensor.validator.utils.load_weights.load_token_config",
                mocks["load_token"],
            ),
        ):
            result = runner.invoke(cli, ["miner", "advisor", "--pat", "ghp_test"])

        assert result.exit_code == 0
        assert "Miner Advisor" in result.output
