"""Per-repository eligibility coverage for finalize_miner_scores.

Each repository gates and scores independently from only its own PRs, against
its own resolved eligibility config. Work in one repo never unlocks or
penalizes another.
"""

from __future__ import annotations

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.utils.mirror.models import MirrorPullRequest
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.validator.oss_contributions.scoring import finalize_miner_scores
from gittensor.validator.utils.load_weights import RepoEligibilityConfig, RepositoryConfig


def _mirror_pr(repo: str, number: int, state: str = 'MERGED') -> ScoredPR:
    merged_at = '2026-04-18T10:00:00Z' if state == 'MERGED' else None
    closed_at = '2026-04-18T10:00:00Z' if state in ('MERGED', 'CLOSED') else None
    pr = MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': number,
            'title': f'PR {number}',
            'body': 'body',
            'state': state,
            'author_github_id': '218712309',
            'author_login': 'miner',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-15T00:00:00Z',
            'closed_at': closed_at,
            'merged_at': merged_at,
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': 1.0 if state == 'MERGED' else None,
            'merged_by_login': 'maintainer' if state == 'MERGED' else None,
            'base_ref': 'main',
            'head_ref': 'feature/foo',
            'head_repo_full_name': repo,
            'default_branch': 'main',
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 1,
            'deletions': 0,
            'commits_count': 1,
            'scoring_data_stored': True,
            'review_summary': {'maintainer_changes_requested_count': 0, 'approved_count': 1},
            'labels': [],
            'linked_issues': [],
        }
    )
    return ScoredPR(pr=pr)


def _merged(repo: str, number: int, base: float = 10.0, token: float = 10.0) -> ScoredPR:
    pr = _mirror_pr(repo, number)
    pr.base_score = base
    pr.token_score = token
    return pr


def _gate_repo() -> RepositoryConfig:
    """A repo on a relaxed eligibility gate for the volume-aware credibility formula.

    ``min_credibility`` is relaxed from the default 0.80 to account for the
    Beta prior (PRIOR_K=2) in the new volume-aware formula (issue #1340).
    """
    return RepositoryConfig(
        emission_share=1.0,
        eligibility=RepoEligibilityConfig(min_valid_merged_prs=3, min_credibility=0.65),
    )


def test_eligibility_does_not_pool_across_repos():
    """3 valid merged PRs in repo A + 2 in repo B: eligible in A, not B."""
    repo_a = [_merged('foo/a', n) for n in range(1, 4)]
    repo_b = [_merged('foo/b', n) for n in range(10, 12)]

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.merged_prs = repo_a + repo_b

    finalize_miner_scores({1: evaluation}, {'foo/a': _gate_repo(), 'foo/b': _gate_repo()})

    assert evaluation.repo_evaluations['foo/a'].is_eligible is True
    assert evaluation.repo_evaluations['foo/b'].is_eligible is False
    assert all(pr.earned_score == 10.0 for pr in repo_a)
    assert all(pr.earned_score == 0.0 for pr in repo_b)
    assert evaluation.total_score == 30.0
    assert evaluation.is_eligible is True  # eligible in at least one repo


def test_zeroed_thresholds_repo_has_no_gate():
    """A repo with zeroed thresholds scores a miner the default gate would reject."""
    pr = _merged('foo/open', 1)
    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.merged_prs = [pr]

    no_gate = RepositoryConfig(
        emission_share=1.0,
        eligibility=RepoEligibilityConfig(min_valid_merged_prs=0, min_credibility=0.0),
    )
    finalize_miner_scores({1: evaluation}, {'foo/open': no_gate})

    assert evaluation.repo_evaluations['foo/open'].is_eligible is True
    assert pr.earned_score == 10.0  # single PR, credibility 1.0


def test_per_repo_credibility_multiplier():
    """Credibility gates eligibility but no longer applies as a per-PR multiplier."""
    merged = [_merged('foo/a', n) for n in range(1, 5)]
    closed = [_mirror_pr('foo/a', 99, state='CLOSED')]

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.merged_prs = merged
    evaluation.closed_prs = closed

    finalize_miner_scores({1: evaluation}, {'foo/a': _gate_repo()})

    # credibility = (4 + 2) / (4 + 1 + 4) = 6/9 = 0.67 (volume-aware, issue #1340)
    assert evaluation.repo_evaluations['foo/a'].is_eligible is True
    assert evaluation.repo_evaluations['foo/a'].credibility == pytest.approx(0.6667, abs=1e-3)
    # credibility is gate-only — no longer a per-PR multiplier
    assert all(pr.earned_score == 10.0 for pr in merged)


def test_open_pr_spam_is_scoped_per_repo():
    """Excess open PRs in one repo do not spam-penalize another repo's earnings."""
    repo_a = [_merged('foo/a', n) for n in range(1, 4)]
    repo_b = [_merged('foo/b', n) for n in range(1, 4)]
    # repo B carries open PRs well past its base threshold of 2
    repo_b_open = [_mirror_pr('foo/b', n, state='OPEN') for n in range(50, 60)]

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.merged_prs = repo_a + repo_b
    evaluation.open_prs = repo_b_open

    finalize_miner_scores({1: evaluation}, {'foo/a': _gate_repo(), 'foo/b': _gate_repo()})

    assert all(pr.open_pr_spam_multiplier == 1.0 for pr in repo_a)
    assert all(pr.earned_score == 10.0 for pr in repo_a)
    assert all(pr.open_pr_spam_multiplier == 0.0 for pr in repo_b)
    assert all(pr.earned_score == 0.0 for pr in repo_b)


def test_open_pr_collateral_is_scoped_per_repo():
    """Open-PR collateral in one repo does not reduce another repo's earnings."""
    repo_a = [_merged('foo/a', n) for n in range(1, 4)]
    repo_b_open = [_mirror_pr('foo/b', n, state='OPEN') for n in range(50, 53)]
    for pr in repo_b_open:
        pr.base_score = 10.0

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.merged_prs = repo_a
    evaluation.open_prs = repo_b_open

    finalize_miner_scores({1: evaluation}, {'foo/a': _gate_repo(), 'foo/b': _gate_repo()})

    assert evaluation.repo_evaluations['foo/a'].total_score == 30.0
    assert evaluation.repo_evaluations['foo/b'].total_collateral_score > 0.0
    assert evaluation.repo_evaluations['foo/b'].total_score == 0.0
    assert evaluation.total_score == 30.0


def test_repo_evaluations_recorded_for_every_touched_repo():
    """finalize_miner_scores records a RepoEvaluation per repo the miner touched."""
    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.merged_prs = [_merged('foo/a', 1), _merged('foo/b', 1)]
    evaluation.open_prs = [_mirror_pr('foo/c', 1, state='OPEN')]

    finalize_miner_scores(
        {1: evaluation},
        {'foo/a': _gate_repo(), 'foo/b': _gate_repo(), 'foo/c': _gate_repo()},
    )

    assert set(evaluation.repo_evaluations) == {'foo/a', 'foo/b', 'foo/c'}
    assert evaluation.repo_evaluations['foo/a'].total_merged_prs == 1
    assert evaluation.repo_evaluations['foo/c'].total_open_prs == 1
