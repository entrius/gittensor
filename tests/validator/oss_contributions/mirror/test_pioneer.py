"""Unit tests for the unified ``calculate_pioneer_dividends`` exercising
``merged_prs`` (ScoredPR shape).

Parallels tests/validator/test_pioneer_dividend.py, which exercises the same
function over legacy ``merged_pull_requests``.
"""

import pytest

pioneer_module = pytest.importorskip('gittensor.validator.oss_contributions.scoring')
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
classes = pytest.importorskip('gittensor.classes')

calculate_pioneer_dividends = pioneer_module.calculate_pioneer_dividends
ScoredPR = scored_pr_module.ScoredPR
MirrorPullRequest = mirror_models.MirrorPullRequest
MinerEvaluation = classes.MinerEvaluation


def _scored(
    pr_number: int,
    merged_at: str = '2026-04-15T00:00:00Z',
    repo: str = 'entrius/gittensor-ui',
    earned_score: float = 10.0,
    token_score: float = 100.0,
) -> ScoredPR:
    pr = MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': pr_number,
            'title': 't',
            'body': 'b',
            'state': 'MERGED',
            'author_github_id': '1',
            'author_login': 'a',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-10T00:00:00Z',
            'closed_at': merged_at,
            'merged_at': merged_at,
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': 1.0,
            'merged_by_login': 'm',
            'base_ref': 'test',
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 1,
            'deletions': 0,
            'commits_count': 1,
            'scoring_data_stored': True,
            'review_summary': {'maintainer_changes_requested_count': 0},
            'labels': [],
            'linked_issues': [],
        }
    )
    scored = ScoredPR(pr=pr)
    scored.token_score = token_score
    scored.earned_score = earned_score
    return scored


def _eval_with(uid: int, scored_prs: list) -> MinerEvaluation:
    me = MinerEvaluation(uid=uid, hotkey=f'hk{uid}', github_id=str(uid))
    me.merged_prs = scored_prs
    return me


class TestMirrorPioneer:
    def test_earliest_merged_gets_rank_1(self):
        uid1_pr = _scored(pr_number=1, merged_at='2026-04-10T00:00:00Z')
        uid2_pr = _scored(pr_number=2, merged_at='2026-04-12T00:00:00Z')
        miner_evals = {
            1: _eval_with(1, [uid1_pr]),
            2: _eval_with(2, [uid2_pr]),
        }
        calculate_pioneer_dividends(miner_evals)

        assert uid1_pr.pioneer_rank == 1
        assert uid2_pr.pioneer_rank == 2

    def test_pioneer_receives_dividend(self):
        pioneer_pr = _scored(pr_number=1, merged_at='2026-04-10T00:00:00Z', earned_score=5.0)
        follower_pr = _scored(pr_number=2, merged_at='2026-04-12T00:00:00Z', earned_score=10.0)
        miner_evals = {
            1: _eval_with(1, [pioneer_pr]),
            2: _eval_with(2, [follower_pr]),
        }
        calculate_pioneer_dividends(miner_evals)

        # Pioneer dividend is some positive value (rate varies by position)
        assert pioneer_pr.pioneer_dividend > 0
        # earned_score gets incremented by dividend
        assert pioneer_pr.earned_score == round(5.0 + pioneer_pr.pioneer_dividend, 2)

    def test_ineligible_pr_skipped(self):
        """PRs that fail is_pioneer_eligible (e.g. token_score below threshold)
        are excluded from pioneer ranking."""
        low_score = _scored(pr_number=1, token_score=1.0)  # below MIN_TOKEN_SCORE_FOR_BASE_SCORE (5)
        high_score = _scored(pr_number=2, token_score=100.0)
        miner_evals = {
            1: _eval_with(1, [low_score]),
            2: _eval_with(2, [high_score]),
        }
        calculate_pioneer_dividends(miner_evals)

        # low_score didn't participate; high_score is the only/pioneer
        assert low_score.pioneer_rank == 0  # default, never touched
        assert high_score.pioneer_rank == 1

    def test_per_repo_isolation(self):
        """Pioneer is per-repo. Different repos get independent rankings."""
        repo_a_uid1 = _scored(pr_number=1, repo='foo/a', merged_at='2026-04-10T00:00:00Z')
        repo_b_uid1 = _scored(pr_number=2, repo='foo/b', merged_at='2026-04-20T00:00:00Z')
        miner_evals = {1: _eval_with(1, [repo_a_uid1, repo_b_uid1])}

        calculate_pioneer_dividends(miner_evals)

        # Single miner on each repo — each is their own pioneer (rank 1)
        assert repo_a_uid1.pioneer_rank == 1
        assert repo_b_uid1.pioneer_rank == 1

    def test_empty_evaluations_no_crash(self):
        calculate_pioneer_dividends({})  # should not raise

    def test_no_pioneer_eligible_prs_no_dividend(self):
        pr = _scored(pr_number=1, token_score=0.0)
        miner_evals = {1: _eval_with(1, [pr])}
        calculate_pioneer_dividends(miner_evals)
        assert pr.pioneer_dividend == 0.0
        assert pr.pioneer_rank == 0
