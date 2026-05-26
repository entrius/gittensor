# Entrius 2025

"""Tests for `gitt miner score` (validator-pipeline e2e for one miner)."""

import json
from datetime import datetime, timezone
from typing import Dict, Optional
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.miner_commands.score import _DEV_HOTKEY, _DEV_UID


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def miner_eval_factory():
    """Build a populated MinerEvaluation for a UID without running real scoring."""

    def _make(uid: int = 5, hotkey: str = 'dev', github_id: str = '12345', failed_reason=None, **overrides):
        from gittensor.classes import MinerEvaluation

        evaluation = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id, failed_reason=failed_reason)
        for key, value in overrides.items():
            setattr(evaluation, key, value)
        return evaluation

    return _make


def _patch_pipeline(
    uid: int,
    miner_evaluation,
    blended: float = 0.3,
    oss_side_effect=None,
    master_repos: Optional[Dict] = None,
    maintainer_uids_by_repo: Optional[Dict] = None,
):
    """Mock the three forward entry points; `oss_side_effect` captures call args."""
    miner_evaluations = {uid: miner_evaluation}

    final_rewards = np.array([blended])

    if oss_side_effect is not None:
        oss_patch = patch('gittensor.validator.forward.oss_contributions', side_effect=oss_side_effect)
    else:
        oss_patch = patch(
            'gittensor.validator.forward.oss_contributions',
            new=AsyncMock(return_value=(miner_evaluations, set(), set())),
        )

    return [
        oss_patch,
        patch('gittensor.validator.forward.issue_discovery', new=AsyncMock(return_value=None)),
        patch('gittensor.validator.emission_allocation.blend_emission_pools', return_value=final_rewards),
        patch(
            'gittensor.validator.forward.build_maintainer_uids_by_repo',
            return_value=maintainer_uids_by_repo or {},
        ),
        patch('gittensor.validator.utils.load_weights.load_master_repo_weights', return_value=master_repos or {}),
        patch('gittensor.validator.utils.load_weights.load_programming_language_weights', return_value={}),
        patch('gittensor.validator.utils.load_weights.load_token_config', return_value=_stub_token_config()),
    ]


def _stub_token_config():
    from gittensor.validator.utils.load_weights import TokenConfig

    return TokenConfig(structural_bonus={}, leaf_tokens={}, language_configs={})


def _make_scored_mirror_pr(
    repo_full_name: str = 'octo/repo',
    pr_number: int = 42,
    state: str = 'MERGED',
    base_score: float = 12.5,
    earned_score: float = 9.75,
    token_score: float = 33.0,
    label: str = 'feature',
):
    """Build a populated ScoredPR minimally sufficient for serialization."""
    from gittensor.utils.mirror.models import MirrorPullRequest, MirrorReviewSummary
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR

    now = datetime.now(timezone.utc)
    pr = MirrorPullRequest(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        title='add feature',
        body=None,
        state=state,
        author_github_id='12345',
        author_login='miner',
        author_association='CONTRIBUTOR',
        created_at=now,
        closed_at=None,
        merged_at=now if state == 'MERGED' else None,
        last_edited_at=None,
        edited_after_merge=False,
        hours_since_merge=0.0,
        merged_by_login='maintainer',
        base_ref='main',
        head_ref='feature',
        head_repo_full_name=repo_full_name,
        default_branch='main',
        head_sha='abc',
        base_sha='def',
        merge_base_sha='def',
        additions=10,
        deletions=2,
        commits_count=1,
        scoring_data_stored=True,
        review_summary=MirrorReviewSummary(),
    )
    return ScoredPR(
        pr=pr,
        base_score=base_score,
        earned_score=earned_score,
        token_score=token_score,
        label=label,
    )


def _make_discovered_issue(repo_full_name: str = 'octo/repo', number: int = 7, earned_score: float = 8.0):
    from gittensor.classes import Issue

    return Issue(
        number=number,
        pr_number=number + 1000,
        repository_full_name=repo_full_name,
        title='discovered',
        discovery_earned_score=earned_score,
    )


def _repo_evaluations(repo_full_name: str, total_score: float) -> Dict:
    """Per-repo rollup the real pipeline writes into MinerEvaluation.repo_evaluations;
    _collect_repo_pr_scores reads total_score from here."""
    from gittensor.classes import RepoEvaluation

    return {repo_full_name: RepoEvaluation(repository_full_name=repo_full_name, total_score=total_score)}


class TestScoreCommand:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'score', '--help'])
        assert result.exit_code == 0
        assert 'validator scoring pipeline' in result.output

    def test_missing_pat_exits(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'score'])
        assert result.exit_code == 1
        assert 'GITTENSOR_MINER_PAT' in result.output

    def test_e2e_table_output(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            is_eligible=True,
            credibility=0.9,
            base_total_score=20.0,
            total_score=18.0,
            total_token_score=42.0,
            unique_repos_count=2,
        )
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        assert f'Miner UID {_DEV_UID}' in result.output
        assert 'Total earned score' in result.output
        assert 'Final blended reward' in result.output

    def test_e2e_json_output(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            is_eligible=True,
            credibility=0.85,
            base_total_score=20.0,
            total_score=18.0,
        )
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation, blended=0.3)):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload['success'] is True
        assert payload['miner_evaluation']['uid'] == _DEV_UID
        assert payload['miner_evaluation']['is_eligible'] is True
        assert payload['miner_evaluation']['credibility'] == 0.85
        assert payload['rewards']['blended_final'] == 0.3
        assert payload['allocation_breakdown'] == []

    def test_json_output_includes_repo_allocation_breakdown(self, runner, miner_eval_factory):
        from gittensor.validator.utils.load_weights import RepositoryConfig

        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            merged_prs=[_make_scored_mirror_pr(repo_full_name='octo/repo', earned_score=42.5)],
            issue_discovery_issues=[_make_discovered_issue(repo_full_name='octo/repo', earned_score=8.0)],
            repo_evaluations=_repo_evaluations('octo/repo', total_score=42.5),
        )
        master_repos = {'octo/repo': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.25)}
        with _multi_patch(
            _patch_pipeline(
                uid=_DEV_UID,
                miner_evaluation=evaluation,
                blended=0.3,
                master_repos=master_repos,
            )
        ):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        rows = payload['allocation_breakdown']
        assert len(rows) == 1
        row = rows[0]
        assert row['repository_full_name'] == 'octo/repo'
        assert row['emission_share'] == 0.2
        assert row['issue_discovery_share'] == 0.25
        assert row['repo_slice'] == 0.18
        assert row['pr_slice'] == 0.135
        assert row['issue_discovery_slice'] == 0.045
        assert row['pr_score'] == 42.5
        assert row['issue_discovery_score'] == 8.0
        assert row['pr_reward'] == 0.135
        assert row['issue_discovery_reward'] == 0.045
        assert row['total_reward'] == 0.18
        assert row['recycled'] is False

    def test_pat_never_appears_in_json(self, runner, miner_eval_factory):
        """The introspection-based serializer relies on _EVAL_SKIP to redact secrets;
        guard against it accidentally leaking github_pat into JSON output."""
        evaluation = miner_eval_factory(uid=_DEV_UID, github_pat='ghp_should_not_leak')
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--pat', 'ghp_should_not_leak', '--json'],
                env={},
            )
        assert result.exit_code == 0, result.output
        assert 'ghp_should_not_leak' not in result.output
        payload = json.loads(result.output)
        assert 'github_pat' not in payload['miner_evaluation']

    def test_failed_reason_renders_in_table(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(uid=_DEV_UID, failed_reason=f'No stored PAT for miner {_DEV_UID}')
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        assert 'failed_reason' in result.output
        assert 'No stored PAT' in result.output

    def test_failed_reason_in_json(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(uid=_DEV_UID, failed_reason='whatever')
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload['miner_evaluation']['failed_reason'] == 'whatever'

    def test_pipeline_called_with_stub_validator(self, runner, miner_eval_factory):
        """The dev tool must wire stub `self.metagraph.hotkeys[uid]` for get_rewards."""
        evaluation = miner_eval_factory(uid=_DEV_UID, hotkey=_DEV_HOTKEY, github_id='99')
        captured = {}

        async def _capture_oss(self, miner_uids, *args, **kwargs):
            captured['hotkey_at_uid'] = self.metagraph.hotkeys[_DEV_UID]
            captured['miner_uids'] = miner_uids
            return {_DEV_UID: evaluation}, set(), set()

        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation, oss_side_effect=_capture_oss)):
            result = runner.invoke(cli, ['miner', 'score'], env={'GITTENSOR_MINER_PAT': 'ghp_dummy'})
        assert result.exit_code == 0, result.output
        assert captured['hotkey_at_uid'] == _DEV_HOTKEY
        assert captured['miner_uids'] == {_DEV_UID}

    def test_real_oss_pipeline_accepts_minimal_stub_validator(self, runner, miner_eval_factory):
        """Regression: single-miner get_rewards does not require evaluation_cache."""
        evaluation = miner_eval_factory(uid=_DEV_UID, hotkey=_DEV_HOTKEY, github_id='0')

        async def _evaluate(uid, hotkey, pat, *_args, **_kwargs):
            assert uid == _DEV_UID
            assert hotkey == _DEV_HOTKEY
            assert pat == 'ghp_dummy'
            return evaluation

        evaluate_mock = AsyncMock(side_effect=_evaluate)
        with patch('gittensor.validator.oss_contributions.reward.evaluate_miners_pull_requests', new=evaluate_mock):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )

        assert result.exit_code == 0, result.output
        assert evaluate_mock.await_count == 1
        payload = json.loads(result.output)
        assert payload['miner_evaluation']['uid'] == _DEV_UID

    def test_populated_mirror_prs_render_in_json(self, runner, miner_eval_factory):
        """Populated mirror PRs must flatten into the JSON shape via _serialize_evaluation."""
        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            is_eligible=True,
            credibility=0.9,
            merged_prs=[_make_scored_mirror_pr()],
        )
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        merged = payload['miner_evaluation']['merged_pull_requests']
        assert len(merged) == 1
        row = merged[0]
        assert row['repository_full_name'] == 'octo/repo'
        assert row['number'] == 42
        assert row['pr_state'] == 'MERGED'
        assert row['base_score'] == 12.5
        assert row['earned_score'] == 9.75
        assert row['token_score'] == 33.0
        assert row['label'] == 'feature'
        # Raw nested payloads and legacy breakdown fields must not leak.
        assert 'pr' not in row
        assert 'files' not in row
        assert 'source' not in row
        assert 'repo_weight_multiplier' not in row

    def test_populated_mirror_prs_render_in_table(self, runner, miner_eval_factory):
        """The per-PR table must surface a row for each populated mirror PR."""
        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            is_eligible=True,
            credibility=0.9,
            merged_prs=[_make_scored_mirror_pr()],
        )
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        assert 'octo/repo#42' in result.output
        assert 'Per-PR breakdown' in result.output

    def test_allocation_breakdown_renders_in_table(self, runner, miner_eval_factory):
        from gittensor.validator.utils.load_weights import RepositoryConfig

        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            merged_prs=[_make_scored_mirror_pr(repo_full_name='octo/repo', earned_score=42.5)],
            issue_discovery_issues=[_make_discovered_issue(repo_full_name='octo/repo', earned_score=8.0)],
            repo_evaluations=_repo_evaluations('octo/repo', total_score=42.5),
        )
        master_repos = {'octo/repo': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.25)}
        with _multi_patch(
            _patch_pipeline(
                uid=_DEV_UID,
                miner_evaluation=evaluation,
                master_repos=master_repos,
            )
        ):
            result = runner.invoke(
                cli,
                ['miner', 'score'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )

        assert result.exit_code == 0, result.output
        assert 'Repo allocation breakdown' in result.output
        assert 'octo/repo' in result.output
        assert '0.135000' in result.output
        assert '0.045000' in result.output

    def test_allocation_breakdown_includes_maintainer_carve_out(self, runner, miner_eval_factory):
        """When the dev UID is a registered maintainer of a maintainer_cut repo,
        the breakdown must surface the carve-out and roll it into total_reward."""
        from gittensor.validator.utils.load_weights import RepositoryConfig

        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            merged_prs=[_make_scored_mirror_pr(repo_full_name='octo/repo', earned_score=42.5)],
            repo_evaluations=_repo_evaluations('octo/repo', total_score=42.5),
        )
        master_repos = {
            'octo/repo': RepositoryConfig(
                emission_share=0.2,
                issue_discovery_share=0.0,
                maintainer_cut=0.5,
            )
        }
        with _multi_patch(
            _patch_pipeline(
                uid=_DEV_UID,
                miner_evaluation=evaluation,
                master_repos=master_repos,
                maintainer_uids_by_repo={'octo/repo': [_DEV_UID]},
            )
        ):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        rows = payload['allocation_breakdown']
        assert len(rows) == 1
        row = rows[0]
        assert row['repository_full_name'] == 'octo/repo'
        assert row['maintainer_cut'] == 0.5
        assert row['repo_slice'] == 0.18
        assert row['maintainer_carve_out'] == 0.09
        assert row['maintainer_reward'] == 0.09
        # PR scoring receives the remaining 0.09 after the carve-out
        assert row['pr_slice'] == 0.09
        assert row['pr_reward'] == 0.09
        assert row['issue_discovery_reward'] == 0
        assert row['total_reward'] == 0.18
        assert row['recycled'] is False

    def test_pat_storage_load_all_pats_is_patched(self, runner, miner_eval_factory):
        """The injected PAT snapshot must override pat_storage.load_all_pats()."""
        evaluation = miner_eval_factory(uid=_DEV_UID)
        captured = {}

        async def _capture_oss(self, miner_uids, *args, **kwargs):
            from gittensor.validator.oss_contributions.reward import pat_storage

            captured['pats'] = pat_storage.load_all_pats()
            return {_DEV_UID: evaluation}, set(), set()

        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation, oss_side_effect=_capture_oss)):
            result = runner.invoke(cli, ['miner', 'score', '--pat', 'ghp_injected'], env={})
        assert result.exit_code == 0, result.output
        assert captured['pats'] == [{'uid': _DEV_UID, 'hotkey': _DEV_HOTKEY, 'pat': 'ghp_injected'}]


def _multi_patch(patches):
    """contextlib.ExitStack-style stack for a flat list of patch context managers."""
    import contextlib

    stack = contextlib.ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack
