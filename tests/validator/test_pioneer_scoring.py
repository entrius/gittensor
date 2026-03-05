from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from gittensor.classes import MinerEvaluation, PRState, PullRequest
from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE, PIONEER_BASE_BONUS
from gittensor.validator.configurations.tier_config import TIERS, Tier
from gittensor.validator.evaluation.scoring import apply_pioneer_mechanism, finalize_miner_scores


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _mk_pr(
    *,
    number: int,
    repo: str,
    uid: int,
    merged_at: datetime,
    created_at: datetime | None,
    token_score: float = 50.0,
    tier_ok: bool = True,
    base_score: float = 10.0,
) -> PullRequest:
    return PullRequest(
        number=number,
        repository_full_name=repo,
        uid=uid,
        hotkey=f'hk-{uid}',
        github_id=str(uid),
        title=f'PR #{number}',
        author_login=f'user-{uid}',
        merged_at=merged_at,
        created_at=created_at,
        pr_state=PRState.MERGED,
        repository_tier_configuration=TIERS[Tier.BRONZE] if tier_ok else None,
        token_score=token_score,
        base_score=base_score,
    )


def _evals_from_prs(prs: list[PullRequest]) -> dict[int, MinerEvaluation]:
    grouped: dict[int, list[PullRequest]] = {}
    for pr in prs:
        grouped.setdefault(pr.uid, []).append(pr)

    evals: dict[int, MinerEvaluation] = {}
    for uid, uid_prs in grouped.items():
        ev = MinerEvaluation(uid=uid, hotkey=f'hk-{uid}', github_id=str(uid), merged_pull_requests=uid_prs)
        ev.unique_repos_contributed_to = {pr.repository_full_name for pr in uid_prs}
        evals[uid] = ev
    return evals


def _pioneer_summary(evals: dict[int, MinerEvaluation]) -> list[tuple]:
    rows: list[tuple] = []
    for uid, ev in sorted(evals.items()):
        for pr in sorted(ev.merged_pull_requests, key=lambda x: (x.repository_full_name, x.number)):
            rows.append(
                (
                    uid,
                    pr.repository_full_name,
                    pr.number,
                    pr.pioneer_rank,
                    round(pr.pioneer_multiplier, 8),
                    pr.is_untouched_in_lookback_window,
                )
            )
    return rows


# `is_untouched_in_lookback_window` is produced by deterministic merged-event replay:
# later same-repo candidates in a cycle can become False once an earlier merge is observed.
def test_first_on_untouched_repo_gets_bonus():
    candidate = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=_dt(2026, 1, 10), created_at=_dt(2026, 1, 8))
    evals = _evals_from_prs([candidate])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert candidate.is_untouched_in_lookback_window is True
    assert candidate.pioneer_rank == 1
    assert candidate.pioneer_multiplier == pytest.approx(1.0 + PIONEER_BASE_BONUS)


def test_repo_not_untouched_when_recent_merge_exists():
    t = _dt(2026, 1, 10)
    candidate = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    history = [
        _mk_pr(
            number=100,
            repo='o/r1',
            uid=99,
            merged_at=t - timedelta(days=10),
            created_at=t - timedelta(days=11),
        )
    ]
    evals = _evals_from_prs([candidate])

    apply_pioneer_mechanism(evals, merged_history=history)

    assert candidate.is_untouched_in_lookback_window is False
    assert candidate.pioneer_rank == 0
    assert candidate.pioneer_multiplier == 1.0


def test_exact_90_day_boundary_is_eligible():
    t = _dt(2026, 1, 10)
    candidate = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    history = [
        _mk_pr(
            number=100,
            repo='o/r1',
            uid=99,
            merged_at=t - timedelta(days=90),
            created_at=t - timedelta(days=91),
        )
    ]
    evals = _evals_from_prs([candidate])

    apply_pioneer_mechanism(evals, merged_history=history)

    assert candidate.is_untouched_in_lookback_window is True
    assert candidate.pioneer_rank == 1


def test_first_candidate_wins_when_merged_at_ties():
    t = _dt(2026, 1, 10, 12, 0)
    first = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=t, created_at=_dt(2026, 1, 9, 8))
    second = _mk_pr(number=2, repo='o/r1', uid=2, merged_at=t, created_at=_dt(2026, 1, 9, 9))
    evals = _evals_from_prs([first, second])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert first.is_untouched_in_lookback_window is True
    assert second.is_untouched_in_lookback_window is False
    assert first.pioneer_rank == 1
    assert second.pioneer_rank == 0


def test_null_created_at_sorts_last():
    t = _dt(2026, 1, 10, 12, 0)
    with_created_at = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=t, created_at=_dt(2026, 1, 9, 8))
    without_created_at = _mk_pr(number=2, repo='o/r1', uid=2, merged_at=t, created_at=None)
    evals = _evals_from_prs([with_created_at, without_created_at])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert with_created_at.is_untouched_in_lookback_window is True
    assert without_created_at.is_untouched_in_lookback_window is False


def test_low_quality_or_missing_tier_not_eligible():
    t = _dt(2026, 1, 10)
    low_quality = _mk_pr(
        number=1,
        repo='o/r1',
        uid=1,
        merged_at=t,
        created_at=t - timedelta(days=1),
        token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 0.01,
    )
    no_tier = _mk_pr(number=2, repo='o/r2', uid=1, merged_at=t, created_at=t - timedelta(days=1), tier_ok=False)
    evals = _evals_from_prs([low_quality, no_tier])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert low_quality.is_untouched_in_lookback_window is False
    assert no_tier.is_untouched_in_lookback_window is False
    assert low_quality.pioneer_rank == 0
    assert no_tier.pioneer_rank == 0


def test_same_pr_row_in_history_is_ignored():
    t = _dt(2026, 1, 10)
    candidate = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    duplicate_in_history = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    evals = _evals_from_prs([candidate])

    apply_pioneer_mechanism(evals, merged_history=[duplicate_in_history])

    assert candidate.is_untouched_in_lookback_window is True
    assert candidate.pioneer_rank == 1


def test_one_representative_per_repo_uid():
    first = _mk_pr(number=1, repo='o/r1', uid=1, merged_at=_dt(2026, 1, 1), created_at=_dt(2025, 12, 30))
    later_same_uid = _mk_pr(number=2, repo='o/r1', uid=1, merged_at=_dt(2026, 5, 1), created_at=_dt(2026, 4, 28))
    # Place this candidate beyond lookback from the prior merge so it remains untouched-eligible.
    other_uid = _mk_pr(number=3, repo='o/r1', uid=2, merged_at=_dt(2026, 8, 5), created_at=_dt(2026, 8, 1))
    evals = _evals_from_prs([first, later_same_uid, other_uid])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert first.pioneer_rank == 1
    assert other_uid.pioneer_rank == 2
    assert later_same_uid.pioneer_rank == 0


def test_follower_stays_at_one():
    t = _dt(2026, 1, 1, 12)
    winner = _mk_pr(number=1, repo='o/repo-a', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    follower = _mk_pr(number=2, repo='o/repo-a', uid=2, merged_at=t + timedelta(hours=1), created_at=t)
    evals = _evals_from_prs([winner, follower])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert winner.pioneer_rank == 1
    assert winner.pioneer_multiplier == pytest.approx(1.0 + PIONEER_BASE_BONUS)
    assert follower.pioneer_multiplier == 1.0


def test_same_uid_wins_multiple_repos_gets_same_multiplier():
    p1 = _mk_pr(number=1, repo='o/r1', uid=7, merged_at=_dt(2026, 1, 1), created_at=_dt(2025, 12, 30))
    p2 = _mk_pr(number=2, repo='o/r2', uid=7, merged_at=_dt(2026, 1, 2), created_at=_dt(2025, 12, 31))
    evals = _evals_from_prs([p1, p2])

    apply_pioneer_mechanism(evals, merged_history=[])

    assert p1.pioneer_rank == 1
    assert p2.pioneer_rank == 1
    assert p1.pioneer_multiplier == pytest.approx(p2.pioneer_multiplier)


def test_multiple_miners_can_pioneer_different_repos_in_same_cycle():
    t = _dt(2026, 1, 1, 12)
    miner1_repo1 = _mk_pr(number=1, repo='o/repo-a', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    miner2_repo2 = _mk_pr(number=2, repo='o/repo-b', uid=2, merged_at=t + timedelta(minutes=5), created_at=t)
    evals = _evals_from_prs([miner1_repo1, miner2_repo2])

    apply_pioneer_mechanism(evals, merged_history=[])

    expected = 1.0 + PIONEER_BASE_BONUS
    assert miner1_repo1.pioneer_rank == 1
    assert miner2_repo2.pioneer_rank == 1
    assert miner1_repo1.pioneer_multiplier == pytest.approx(expected)
    assert miner2_repo2.pioneer_multiplier == pytest.approx(expected)


def test_idempotent_and_order_independent():
    t = _dt(2026, 1, 1, 12)
    p1 = _mk_pr(number=1, repo='o/repo-a', uid=1, merged_at=t, created_at=t - timedelta(days=1))
    p2 = _mk_pr(number=2, repo='o/repo-b', uid=1, merged_at=t + timedelta(hours=1), created_at=t)
    p3 = _mk_pr(number=3, repo='o/repo-a', uid=2, merged_at=t + timedelta(hours=2), created_at=t)

    run_a = _evals_from_prs(deepcopy([p1, p2, p3]))
    run_b = _evals_from_prs(deepcopy([p3, p2, p1]))

    apply_pioneer_mechanism(run_a, merged_history=[])
    apply_pioneer_mechanism(run_b, merged_history=[])

    assert _pioneer_summary(run_a) == _pioneer_summary(run_b)


def test_finalize_miner_scores_disables_pioneer_when_history_unavailable(monkeypatch):
    t = _dt(2026, 1, 1, 12)

    monkeypatch.setattr(
        'gittensor.validator.evaluation.scoring.calculate_credibility_per_tier',
        lambda merged_prs, closed_prs: {tier: 1.0 for tier in Tier},
    )

    pioneer_candidate = _mk_pr(number=1, repo='o/repo-a', uid=1, merged_at=t, created_at=t - timedelta(days=1), base_score=10.0)
    evals = _evals_from_prs([pioneer_candidate])

    finalize_miner_scores(evals, merged_history=None)

    assert pioneer_candidate.pioneer_rank == 0
    assert pioneer_candidate.pioneer_multiplier == 1.0
    assert pioneer_candidate.is_untouched_in_lookback_window is False
    assert pioneer_candidate.earned_score == pytest.approx(10.0)
