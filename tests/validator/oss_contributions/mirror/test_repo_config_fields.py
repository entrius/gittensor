"""Regression coverage for mirror-only repository scoring config fields."""

from __future__ import annotations

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.utils.mirror.models import MirrorPullRequest
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredMirrorPR
from gittensor.validator.oss_contributions.scoring import finalize_miner_scores
from gittensor.validator.utils.load_weights import RepositoryConfig


def _mirror_pr(
    repo: str,
    number: int,
    *,
    state: str = 'MERGED',
    base_score: float = 10.0,
    token_score: float = 10.0,
) -> ScoredMirrorPR:
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
            'review_summary': {
                'maintainer_changes_requested_count': 0,
                'changes_requested_count': 0,
                'approved_count': 1,
                'commented_count': 0,
            },
            'labels': [],
            'linked_issues': [],
        }
    )
    scored = ScoredMirrorPR(pr=pr)
    scored.base_score = base_score
    scored.token_score = token_score
    return scored


def test_ineligible_miner_scores_only_eligibility_disabled_repo():
    opt_out = _mirror_pr('foo/open-door', 1, token_score=0.0)
    gated = _mirror_pr('foo/gated', 2, base_score=50.0, token_score=0.0)

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.mirror_merged_prs = [opt_out, gated]

    repos = {
        'foo/open-door': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=False),
        'foo/gated': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=True),
    }

    finalize_miner_scores({1: evaluation}, repos)

    assert evaluation.is_eligible is False
    assert evaluation.credibility == pytest.approx(1.0)
    assert opt_out.earned_score == pytest.approx(10.0)
    assert opt_out.credibility_multiplier == pytest.approx(1.0)
    assert gated.earned_score == pytest.approx(0.0)
    assert evaluation.base_total_score == pytest.approx(60.0)
    assert evaluation.total_score == pytest.approx(10.0)


def test_miner_with_no_gated_history_scores_eligibility_disabled_repo():
    opt_out = _mirror_pr('foo/open-door', 1, token_score=0.0)

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.mirror_merged_prs = [opt_out]

    repos = {
        'foo/open-door': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=False),
    }

    finalize_miner_scores({1: evaluation}, repos)

    assert evaluation.is_eligible is False
    assert evaluation.credibility == pytest.approx(0.0)
    assert opt_out.earned_score == pytest.approx(10.0)
    assert opt_out.credibility_multiplier == pytest.approx(1.0)
    assert evaluation.total_score == pytest.approx(10.0)


def test_eligibility_disabled_prs_do_not_unlock_gated_repo_rewards():
    opt_out_prs = [_mirror_pr(f'foo/open-door-{number}', number) for number in range(1, 6)]
    gated = _mirror_pr('foo/gated', 100, base_score=50.0)

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.mirror_merged_prs = opt_out_prs + [gated]

    repos = {
        **{
            pr.repository_full_name: RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=False)
            for pr in opt_out_prs
        },
        'foo/gated': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=True),
    }

    finalize_miner_scores({1: evaluation}, repos)

    assert evaluation.is_eligible is False
    assert all(pr.earned_score == pytest.approx(10.0) for pr in opt_out_prs)
    assert gated.earned_score == pytest.approx(0.0)
    assert evaluation.total_score == pytest.approx(50.0)


def test_eligibility_disabled_closed_prs_do_not_penalize_gated_repo_eligibility():
    gated_prs = [_mirror_pr('foo/gated', number) for number in range(1, 6)]
    opt_out_closed_prs = [_mirror_pr('foo/open-door', number, state='CLOSED') for number in range(100, 120)]

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.mirror_merged_prs = gated_prs
    evaluation.mirror_closed_prs = opt_out_closed_prs

    repos = {
        'foo/gated': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=True),
        'foo/open-door': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=False),
    }

    finalize_miner_scores({1: evaluation}, repos)

    assert evaluation.is_eligible is True
    assert evaluation.credibility == pytest.approx(1.0)
    assert all(pr.earned_score == pytest.approx(10.0) for pr in gated_prs)
    assert evaluation.total_score == pytest.approx(50.0)


def test_eligibility_disabled_open_prs_do_not_spam_penalize_gated_rewards():
    gated_prs = [_mirror_pr('foo/gated', number) for number in range(1, 6)]
    opt_out_open_prs = [_mirror_pr('foo/open-door', number, state='OPEN') for number in range(100, 111)]

    evaluation = MinerEvaluation(uid=1, hotkey='hotkey', github_id='218712309')
    evaluation.mirror_merged_prs = gated_prs
    evaluation.mirror_open_prs = opt_out_open_prs

    repos = {
        'foo/gated': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=True),
        'foo/open-door': RepositoryConfig(weight=1.0, mirror_enabled=True, eligibility_mode=False),
    }

    finalize_miner_scores({1: evaluation}, repos)

    assert evaluation.is_eligible is True
    assert all(pr.open_pr_spam_multiplier == pytest.approx(1.0) for pr in gated_prs)
    assert all(pr.earned_score == pytest.approx(10.0) for pr in gated_prs)
    assert evaluation.total_score == pytest.approx(50.0)
