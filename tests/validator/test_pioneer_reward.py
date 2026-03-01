from datetime import datetime, timedelta, timezone

import pytest

from gittensor.classes import MinerEvaluation, PRState
from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE, PIONEER_BASE_BONUS, PIONEER_MULTI_REPO_DECAY_EXPONENT
from gittensor.validator.evaluation.scoring import (
    build_repo_contributor_ordering,
    calculate_pioneer_reward_multiplier,
    collect_repo_pioneer_candidates,
    count_pioneered_repositories,
    finalize_miner_scores,
    is_pioneer_eligible,
)

# =============================================================================
# build_repo_contributor_ordering
# =============================================================================


class TestBuildRepoContributorOrdering:

    def test_empty_evaluations(self):
        ordering, pioneer_prs = build_repo_contributor_ordering({})
        assert ordering == {}
        assert pioneer_prs == {}

    def test_single_contributor_is_pioneer(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now)
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr])}
        ordering, pioneer_prs = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1}
        assert pioneer_prs['owner/repo'] == pr.number

    def test_earliest_merge_gets_position_one(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        early = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now - timedelta(days=5))
        late = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[early]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[late]),
        }
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1, 2: 2}

    def test_deterministic_tie_breaking_by_pr_number(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        same_time = now - timedelta(days=1)
        pr_high = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=20, repo='owner/repo', uid=1, merged_at=same_time)
        pr_low = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=3, repo='owner/repo', uid=2, merged_at=same_time)
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_high]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[pr_low]),
        }
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'][2] == 1
        assert ordering['owner/repo'][1] == 2

    def test_multiple_repos_produce_independent_orderings(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr_a = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo-a', uid=1, merged_at=now - timedelta(days=5))
        pr_b = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo-b', uid=2, merged_at=now - timedelta(days=3))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_a]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[pr_b]),
        }
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo-a'] == {1: 1}
        assert ordering['owner/repo-b'] == {2: 1}

    def test_miner_multiple_prs_same_repo_uses_earliest(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        early = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now - timedelta(days=5))
        late = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now - timedelta(days=1))
        other = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=2, merged_at=now - timedelta(days=3))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[early, late]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[other]),
        }
        ordering, pioneer_prs = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1, 2: 2}
        assert pioneer_prs['owner/repo'] == early.number

    def test_miner_same_timestamp_uses_lower_pr_number(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr_high = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=20, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        pr_low = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=5, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        other = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_high, pr_low]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[other]),
        }
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {1: 1, 2: 2}

    def test_skips_prs_without_tier_config(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now)
        pr.repository_tier_configuration = None
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr])}
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering == {}

    def test_skips_prs_without_merged_at(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now)
        pr.merged_at = None
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr])}
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering == {}

    def test_skips_low_quality_prs_for_pioneer_ordering(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        low_quality = pr_factory.create(
            state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1,
            merged_at=now - timedelta(days=2), token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        high_quality = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[low_quality]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[high_quality]),
        }
        ordering, _ = build_repo_contributor_ordering(evals)
        assert ordering['owner/repo'] == {2: 1}


class TestPioneerEligibility:

    def test_requires_tier_and_merge_timestamp(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now)
        assert is_pioneer_eligible(pr)
        pr.repository_tier_configuration = None
        assert not is_pioneer_eligible(pr)
        pr.repository_tier_configuration = bronze_config
        pr.merged_at = None
        assert not is_pioneer_eligible(pr)

    def test_requires_minimum_token_score_threshold(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now)
        pr.token_score = 4.99
        assert not is_pioneer_eligible(pr)
        pr.token_score = 5.0
        assert is_pioneer_eligible(pr)


class TestCollectRepoPioneerCandidates:

    def test_collects_only_eligible_earliest_per_miner(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        low_quality = pr_factory.create(
            state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1,
            merged_at=now - timedelta(days=3), token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        eligible_late = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        other = pr_factory.create(state=PRState.MERGED, tier=bronze_config, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[low_quality, eligible_late]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[other]),
        }
        candidates = collect_repo_pioneer_candidates(evals)
        assert candidates['owner/repo'] == [
            (eligible_late.merged_at, eligible_late.number, 1),
            (other.merged_at, other.number, 2),
        ]


# =============================================================================
# calculate_pioneer_reward_multiplier
# =============================================================================


class TestCalculatePioneerRewardMultiplier:
    """Tests for the pure multiplier function (scalar params, no setup needed)."""

    def test_single_pioneered_repo_gets_full_bonus(self):
        assert calculate_pioneer_reward_multiplier(1) == round(1.0 + PIONEER_BASE_BONUS, 2)

    def test_multi_repo_applies_diminishing_returns(self):
        expected = round(1.0 + (PIONEER_BASE_BONUS / (4**PIONEER_MULTI_REPO_DECAY_EXPONENT)), 2)
        assert calculate_pioneer_reward_multiplier(4) == expected

    def test_higher_repo_count_yields_lower_multiplier(self):
        assert calculate_pioneer_reward_multiplier(1) > calculate_pioneer_reward_multiplier(4)
        assert calculate_pioneer_reward_multiplier(4) > calculate_pioneer_reward_multiplier(9)

    def test_multiplier_always_above_one(self):
        for count in range(1, 100):
            assert calculate_pioneer_reward_multiplier(count) > 1.0

    def test_result_is_rounded_to_two_decimal_places(self):
        for count in (1, 3, 7, 13, 50):
            result = calculate_pioneer_reward_multiplier(count)
            assert result == round(result, 2)

    def test_zero_count_treated_as_one(self):
        assert calculate_pioneer_reward_multiplier(0) == calculate_pioneer_reward_multiplier(1)


class TestCountPioneeredRepositories:

    def test_counts_only_position_one(self):
        ordering = {
            'owner/repo-a': {1: 1, 2: 2},
            'owner/repo-b': {1: 1, 3: 2},
            'owner/repo-c': {2: 1, 1: 2},
        }
        counts = count_pioneered_repositories(ordering)
        assert counts == {1: 2, 2: 1}


class TestPioneerPrNumbers:

    def test_selects_earliest_pr_number_per_repo(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        early = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=5, repo='owner/repo', uid=1, merged_at=now - timedelta(days=3))
        late = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=9, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[early]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[late]),
        }
        _, pioneer_prs = build_repo_contributor_ordering(evals)
        assert pioneer_prs == {'owner/repo': 5}

    def test_low_quality_early_pr_does_not_capture_pioneer_number(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        low_quality = pr_factory.create(
            state=PRState.MERGED, tier=bronze_config, number=5, repo='owner/repo', uid=1,
            merged_at=now - timedelta(days=3), token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        good_follower = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=9, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[low_quality]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[good_follower]),
        }
        _, pioneer_prs = build_repo_contributor_ordering(evals)
        assert pioneer_prs == {'owner/repo': 9}


# =============================================================================
# Integration with finalize_miner_scores
# =============================================================================


class TestFinalizeWithPioneerReward:

    def test_pioneer_gets_higher_multiplier_than_follower(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pioneer_pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        follower_pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[follower_pr]),
        }
        finalize_miner_scores(evals)

        pioneer_mult = evals[1].merged_pull_requests[0].pioneer_multiplier
        follower_mult = evals[2].merged_pull_requests[0].pioneer_multiplier
        assert pioneer_mult == round(1.0 + PIONEER_BASE_BONUS, 2)
        assert follower_mult == 1.0
        assert pioneer_mult > follower_mult

    def test_multiple_miners_pioneering_different_repos(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pr_a = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo-a', uid=1, merged_at=now - timedelta(days=3))
        pr_b = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo-b', uid=2, merged_at=now - timedelta(days=2))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pr_a]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[pr_b]),
        }
        finalize_miner_scores(evals)

        assert evals[1].merged_pull_requests[0].pioneer_multiplier == round(1.0 + PIONEER_BASE_BONUS, 2)
        assert evals[2].merged_pull_requests[0].pioneer_multiplier == round(1.0 + PIONEER_BASE_BONUS, 2)

    def test_finalize_only_first_pr_gets_pioneer_bonus_for_repo(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pioneer_first = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        pioneer_follow_up = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo', uid=1, merged_at=now - timedelta(days=1))
        follower = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=12, repo='owner/repo', uid=2, merged_at=now)
        evals = {
            1: MinerEvaluation(
                uid=1,
                hotkey='h1',
                github_id='1',
                merged_pull_requests=[pioneer_first, pioneer_follow_up],
            ),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[follower]),
        }
        finalize_miner_scores(evals)

        miner1_prs = sorted(evals[1].merged_pull_requests, key=lambda pr: pr.number)
        assert miner1_prs[0].pioneer_multiplier == round(1.0 + PIONEER_BASE_BONUS, 2)
        assert miner1_prs[1].pioneer_multiplier == 1.0
        assert evals[2].merged_pull_requests[0].pioneer_multiplier == 1.0

    def test_finalize_applies_multi_repo_diminishing_returns(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        miner_a_prs = [
            pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo-a', uid=1, merged_at=now - timedelta(days=5)),
            pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo-b', uid=1, merged_at=now - timedelta(days=4)),
            pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=12, repo='owner/repo-c', uid=1, merged_at=now - timedelta(days=3)),
            pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=13, repo='owner/repo-d', uid=1, merged_at=now - timedelta(days=2)),
        ]
        miner_b_prs = [
            pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=20, repo='owner/repo-e', uid=2, merged_at=now - timedelta(days=1)),
        ]

        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=miner_a_prs),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=miner_b_prs),
        }
        finalize_miner_scores(evals)

        miner_a_expected = round(1.0 + (PIONEER_BASE_BONUS / (4**PIONEER_MULTI_REPO_DECAY_EXPONENT)), 2)
        for pr in evals[1].merged_pull_requests:
            assert pr.pioneer_multiplier == miner_a_expected

        miner_b_expected = round(1.0 + PIONEER_BASE_BONUS, 2)
        assert evals[2].merged_pull_requests[0].pioneer_multiplier == miner_b_expected
        assert miner_a_expected < miner_b_expected

    def test_no_contributions_leaves_defaults(self):
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1'),
        }
        finalize_miner_scores(evals)
        assert evals[1].total_score == 0.0

    def test_finalize_low_quality_early_merge_does_not_get_pioneer_bonus(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        low_quality_early = pr_factory.create(
            state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo', uid=1,
            merged_at=now - timedelta(days=2), token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        high_quality_late = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[low_quality_early]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[high_quality_late]),
        }
        finalize_miner_scores(evals)
        assert evals[1].merged_pull_requests[0].pioneer_multiplier == 1.0
        assert evals[2].merged_pull_requests[0].pioneer_multiplier == round(1.0 + PIONEER_BASE_BONUS, 2)

    def test_follower_and_ineligible_pr_get_neutral_multiplier(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pioneer_pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        follower_pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo', uid=2, merged_at=now - timedelta(days=1))
        ineligible_pr = pr_factory.create(
            state=PRState.MERGED, tier=bronze_config, number=12, repo='owner/repo', uid=3,
            merged_at=now, token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', github_id='2', merged_pull_requests=[follower_pr]),
            3: MinerEvaluation(uid=3, hotkey='h3', github_id='3', merged_pull_requests=[ineligible_pr]),
        }
        finalize_miner_scores(evals)
        assert evals[2].merged_pull_requests[0].pioneer_multiplier == 1.0
        assert evals[3].merged_pull_requests[0].pioneer_multiplier == 1.0

    def test_only_pioneering_pr_gets_bonus_follow_up_neutral(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)
        pioneer_first = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=10, repo='owner/repo', uid=1, merged_at=now - timedelta(days=2))
        pioneer_follow_up = pr_factory.create(state=PRState.MERGED, tier=bronze_config, number=11, repo='owner/repo', uid=1, merged_at=now)
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', github_id='1', merged_pull_requests=[pioneer_first, pioneer_follow_up]),
        }
        finalize_miner_scores(evals)
        prs = sorted(evals[1].merged_pull_requests, key=lambda p: p.number)
        assert prs[0].pioneer_multiplier == round(1.0 + PIONEER_BASE_BONUS, 2)
        assert prs[1].pioneer_multiplier == 1.0


class TestPioneerIncentiveEvidence:
    """Evidence-oriented tests showing exploration beats pile-on behavior."""

    def test_exploration_yields_higher_network_pioneer_bonus_than_pile_on(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)

        # 5 miners all contribute to the same repo -> only one pioneer bonus applies.
        pile_on_evals = {}
        for uid in range(1, 6):
            pr = pr_factory.create(
                state=PRState.MERGED, tier=bronze_config, number=uid,
                repo='owner/saturated-repo', uid=uid, merged_at=now - timedelta(days=uid),
            )
            pile_on_evals[uid] = MinerEvaluation(uid=uid, hotkey=f'h{uid}', github_id=str(uid), merged_pull_requests=[pr])
        finalize_miner_scores(pile_on_evals)
        pile_on_bonus_total = sum(
            pr.pioneer_multiplier - 1.0
            for ev in pile_on_evals.values()
            for pr in ev.merged_pull_requests
        )

        # 5 miners each pioneer a distinct repo -> each miner gets pioneer bonus.
        explore_evals = {}
        for uid in range(1, 6):
            pr = pr_factory.create(
                state=PRState.MERGED, tier=bronze_config, number=100 + uid,
                repo=f'owner/new-repo-{uid}', uid=uid, merged_at=now - timedelta(days=uid),
            )
            explore_evals[uid] = MinerEvaluation(uid=uid, hotkey=f'he{uid}', github_id=str(1000 + uid), merged_pull_requests=[pr])
        finalize_miner_scores(explore_evals)
        explore_bonus_total = sum(
            pr.pioneer_multiplier - 1.0
            for ev in explore_evals.values()
            for pr in ev.merged_pull_requests
        )

        assert pile_on_bonus_total == pytest.approx(PIONEER_BASE_BONUS)
        assert explore_bonus_total == pytest.approx(PIONEER_BASE_BONUS * 5)
        assert explore_bonus_total > pile_on_bonus_total

    def test_mixed_market_distribution_rewards_breadth_over_concentration(self, pr_factory, bronze_config):
        now = datetime.now(timezone.utc)

        # 20 miners concentrated into 2 repos (10 miners each): only 2 pioneer events.
        concentrated = {}
        for uid in range(1, 21):
            repo_idx = 1 if uid <= 10 else 2
            pr = pr_factory.create(
                state=PRState.MERGED, tier=bronze_config, number=uid,
                repo=f'owner/concentrated-{repo_idx}', uid=uid, merged_at=now - timedelta(days=uid),
            )
            concentrated[uid] = MinerEvaluation(
                uid=uid,
                hotkey=f'hc{uid}',
                github_id=str(uid),
                merged_pull_requests=[pr],
            )
        finalize_miner_scores(concentrated)
        concentrated_bonus_total = sum(
            pr.pioneer_multiplier - 1.0
            for ev in concentrated.values()
            for pr in ev.merged_pull_requests
        )

        # 20 miners distributed across 20 repos: 20 pioneer events.
        distributed = {}
        for uid in range(1, 21):
            pr = pr_factory.create(
                state=PRState.MERGED, tier=bronze_config, number=100 + uid,
                repo=f'owner/distributed-{uid}', uid=uid, merged_at=now - timedelta(days=uid),
            )
            distributed[uid] = MinerEvaluation(
                uid=uid,
                hotkey=f'hd{uid}',
                github_id=str(2000 + uid),
                merged_pull_requests=[pr],
            )
        finalize_miner_scores(distributed)
        distributed_bonus_total = sum(
            pr.pioneer_multiplier - 1.0
            for ev in distributed.values()
            for pr in ev.merged_pull_requests
        )

        assert concentrated_bonus_total == pytest.approx(PIONEER_BASE_BONUS * 2)
        assert distributed_bonus_total == pytest.approx(PIONEER_BASE_BONUS * 20)
        assert distributed_bonus_total > concentrated_bonus_total

    def test_diminishing_returns_still_keep_pioneer_advantage_material(self):
        # With defaults (base=1.2, exponent=0.5), even at 9 pioneered repos:
        # multiplier = 1 + 1.2 / sqrt(9) = 1.4 (still materially above 1.0)
        expected = 1.0 + (PIONEER_BASE_BONUS / (9**PIONEER_MULTI_REPO_DECAY_EXPONENT))
        assert expected == pytest.approx(1.4)
        assert expected > 1.0
