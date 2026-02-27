from datetime import datetime, timedelta, timezone

from gittensor.classes import MinerEvaluation, PRState, PullRequest
from gittensor.constants import PIONEER_BASE_BONUS, PIONEER_MULTI_REPO_DECAY_EXPONENT
from gittensor.validator.configurations.tier_config import TIERS, Tier
from gittensor.validator.evaluation.scoring import (
    build_repo_contributor_ordering,
    count_pioneered_repositories,
    calculate_pioneer_reward_multiplier,
    finalize_miner_scores,
)


def _make_merged_pr(
    *,
    uid: int,
    number: int,
    repo: str,
    merged_at: datetime,
    base_score: float = 10.0,
) -> PullRequest:
    return PullRequest(
        number=number,
        repository_full_name=repo,
        uid=uid,
        hotkey=f'hotkey-{uid}',
        github_id=str(uid),
        title=f'PR {number}',
        author_login=f'user-{uid}',
        merged_at=merged_at,
        created_at=merged_at - timedelta(hours=1),
        pr_state=PRState.MERGED,
        repository_tier_configuration=TIERS[Tier.BRONZE],
        base_score=base_score,
        token_score=20.0,
    )


# =============================================================================
# build_repo_contributor_ordering
# =============================================================================


class TestBuildRepoContributorOrdering:

    def test_empty_evaluations(self):
        assert build_repo_contributor_ordering({}) == {}

    def test_single_contributor_is_pioneer(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now)
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr])}
        ordering = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1}

    def test_earliest_merge_gets_position_one(self):
        now = datetime.now(timezone.utc)
        early = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now - timedelta(days=5))
        late = _make_merged_pr(uid=2, number=11, repo='owner/repo', merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[early]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[late]),
        }
        ordering = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1, 2: 2}

    def test_deterministic_tie_breaking_by_pr_number(self):
        now = datetime.now(timezone.utc)
        same_time = now - timedelta(days=1)
        pr_high = _make_merged_pr(uid=1, number=20, repo='owner/repo', merged_at=same_time)
        pr_low = _make_merged_pr(uid=2, number=3, repo='owner/repo', merged_at=same_time)
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_high]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[pr_low]),
        }
        ordering = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'][2] == 1
        assert ordering['owner/repo'][1] == 2

    def test_multiple_repos_produce_independent_orderings(self):
        now = datetime.now(timezone.utc)
        pr_a = _make_merged_pr(uid=1, number=10, repo='owner/repo-a', merged_at=now - timedelta(days=5))
        pr_b = _make_merged_pr(uid=2, number=11, repo='owner/repo-b', merged_at=now - timedelta(days=3))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_a]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[pr_b]),
        }
        ordering = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo-a'] == {1: 1}
        assert ordering['owner/repo-b'] == {2: 1}

    def test_miner_multiple_prs_same_repo_uses_earliest(self):
        now = datetime.now(timezone.utc)
        early = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now - timedelta(days=5))
        late = _make_merged_pr(uid=1, number=11, repo='owner/repo', merged_at=now - timedelta(days=1))
        other = _make_merged_pr(uid=2, number=12, repo='owner/repo', merged_at=now - timedelta(days=3))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[early, late]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[other]),
        }
        ordering = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1, 2: 2}

    def test_miner_same_timestamp_uses_lower_pr_number(self):
        now = datetime.now(timezone.utc)
        pr_high = _make_merged_pr(uid=1, number=20, repo='owner/repo', merged_at=now - timedelta(days=2))
        pr_low = _make_merged_pr(uid=1, number=5, repo='owner/repo', merged_at=now - timedelta(days=2))
        other = _make_merged_pr(uid=2, number=12, repo='owner/repo', merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_high, pr_low]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[other]),
        }
        ordering = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1, 2: 2}

    def test_skips_prs_without_tier_config(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now)
        pr.repository_tier_configuration = None
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr])}
        assert build_repo_contributor_ordering(evals) == {}

    def test_skips_prs_without_merged_at(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now)
        pr.merged_at = None
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr])}
        assert build_repo_contributor_ordering(evals) == {}


# =============================================================================
# calculate_pioneer_reward_multiplier
# =============================================================================


class TestCalculatePioneerRewardMultiplier:

    def test_pioneer_gets_full_bonus(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now)
        ordering = {'owner/repo': {1: 1}}
        pioneered_counts = {1: 1}
        assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) == 1.0 + PIONEER_BASE_BONUS

    def test_second_contributor_scores_normally(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=2, number=11, repo='owner/repo', merged_at=now)
        ordering = {'owner/repo': {1: 1, 2: 2}}
        pioneered_counts = {1: 1}
        assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) == 1.0

    def test_third_contributor_scores_normally(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=3, number=12, repo='owner/repo', merged_at=now)
        ordering = {'owner/repo': {1: 1, 2: 2, 3: 3}}
        pioneered_counts = {1: 1}
        assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) == 1.0

    def test_multiplier_never_below_one(self):
        now = datetime.now(timezone.utc)
        ordering = {'owner/repo': {uid: uid for uid in range(1, 21)}}
        pioneered_counts = {1: 1}
        for uid in range(1, 21):
            pr = _make_merged_pr(uid=uid, number=uid, repo='owner/repo', merged_at=now)
            assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) >= 1.0

    def test_unknown_repo_returns_neutral(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=1, number=10, repo='unknown/repo', merged_at=now)
        assert calculate_pioneer_reward_multiplier(pr, {}, {}) == 1.0

    def test_uid_not_in_ordering_returns_neutral(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=99, number=10, repo='owner/repo', merged_at=now)
        ordering = {'owner/repo': {1: 1, 2: 2}}
        pioneered_counts = {1: 1}
        assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) == 1.0

    def test_all_prs_from_same_miner_get_same_multiplier(self):
        now = datetime.now(timezone.utc)
        pr_a = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now - timedelta(days=2))
        pr_b = _make_merged_pr(uid=1, number=11, repo='owner/repo', merged_at=now)
        ordering = {'owner/repo': {1: 1, 2: 2}}
        pioneered_counts = {1: 1}
        assert calculate_pioneer_reward_multiplier(pr_a, ordering, pioneered_counts) == calculate_pioneer_reward_multiplier(
            pr_b, ordering, pioneered_counts
        )

    def test_followers_score_normally(self):
        now = datetime.now(timezone.utc)
        ordering = {'owner/repo': {1: 1, 2: 2, 3: 3, 4: 4}}
        pioneered_counts = {1: 1}
        for uid in (2, 3, 4):
            pr = _make_merged_pr(uid=uid, number=uid, repo='owner/repo', merged_at=now)
            assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) == 1.0

    def test_pioneer_bonus_has_multi_repo_diminishing_returns(self):
        now = datetime.now(timezone.utc)
        pr = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now)
        ordering = {'owner/repo': {1: 1}}
        pioneered_counts = {1: 4}
        expected = 1.0 + (PIONEER_BASE_BONUS / (4**PIONEER_MULTI_REPO_DECAY_EXPONENT))
        assert calculate_pioneer_reward_multiplier(pr, ordering, pioneered_counts) == expected


class TestCountPioneeredRepositories:

    def test_counts_only_position_one(self):
        ordering = {
            'owner/repo-a': {1: 1, 2: 2},
            'owner/repo-b': {1: 1, 3: 2},
            'owner/repo-c': {2: 1, 1: 2},
        }
        counts = count_pioneered_repositories(ordering)
        assert counts == {1: 2, 2: 1}


# =============================================================================
# Integration with finalize_miner_scores
# =============================================================================


class TestFinalizeWithPioneerReward:

    def test_pioneer_gets_higher_multiplier_than_follower(self):
        now = datetime.now(timezone.utc)
        pioneer_pr = _make_merged_pr(uid=1, number=10, repo='owner/repo', merged_at=now - timedelta(days=2))
        follower_pr = _make_merged_pr(uid=2, number=11, repo='owner/repo', merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[follower_pr]),
        }
        finalize_miner_scores(evals)

        pioneer_mult = evals[1].merged_pull_requests[0].repository_uniqueness_multiplier
        follower_mult = evals[2].merged_pull_requests[0].repository_uniqueness_multiplier
        assert pioneer_mult == 1.0 + PIONEER_BASE_BONUS
        assert follower_mult == 1.0
        assert pioneer_mult > follower_mult

    def test_multiple_miners_pioneering_different_repos(self):
        now = datetime.now(timezone.utc)
        pr_a = _make_merged_pr(uid=1, number=10, repo='owner/repo-a', merged_at=now - timedelta(days=3))
        pr_b = _make_merged_pr(uid=2, number=11, repo='owner/repo-b', merged_at=now - timedelta(days=2))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_a]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[pr_b]),
        }
        finalize_miner_scores(evals)

        assert evals[1].merged_pull_requests[0].repository_uniqueness_multiplier == 1.0 + PIONEER_BASE_BONUS
        assert evals[2].merged_pull_requests[0].repository_uniqueness_multiplier == 1.0 + PIONEER_BASE_BONUS

    def test_no_contributions_leaves_defaults(self):
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1'),
        }
        finalize_miner_scores(evals)
        assert evals[1].total_score == 0.0
