from __future__ import annotations

from gittensor.classes import MinerEvaluation
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredMirrorPR
from gittensor.validator.oss_contributions.scoring import finalize_miner_scores
from gittensor.utils.mirror.models import MirrorPullRequest


def _mirror_pr(repo_full_name: str, pr_number: int) -> MirrorPullRequest:
    return MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo_full_name,
            'pr_number': pr_number,
            'title': 't',
            'body': 'b',
            'state': 'MERGED',
            'author_github_id': '1',
            'author_login': 'alice',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-01T00:00:00Z',
            'closed_at': '2026-04-18T10:00:00Z',
            'merged_at': '2026-04-18T10:00:00Z',
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': 1.0,
            'merged_by_login': 'maintainer',
            'base_ref': 'main',
            'head_ref': 'feature/pr',
            'head_repo_full_name': repo_full_name,
            'default_branch': 'main',
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 10,
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


def _scored_pr(repo_full_name: str, pr_number: int, *, base_score: float, eligibility_mode: bool) -> ScoredMirrorPR:
    scored = ScoredMirrorPR(pr=_mirror_pr(repo_full_name, pr_number))
    scored.base_score = base_score
    scored.token_score = 10.0
    scored.eligibility_mode = eligibility_mode
    return scored


def test_ungated_mirror_repo_does_not_unlock_gated_repos_in_same_round():
    evaluation = MinerEvaluation(uid=7, hotkey='hk', github_id='gh')
    evaluation.mirror_merged_prs = [
        _scored_pr('gated/repo', 1, base_score=5.0, eligibility_mode=True),
        _scored_pr('gated/repo', 2, base_score=5.0, eligibility_mode=True),
        _scored_pr('gated/repo', 3, base_score=5.0, eligibility_mode=True),
        _scored_pr('gated/repo', 4, base_score=5.0, eligibility_mode=True),
        _scored_pr('ungated/repo', 5, base_score=7.0, eligibility_mode=False),
    ]

    finalize_miner_scores({evaluation.uid: evaluation})

    gated_scores = [pr.earned_score for pr in evaluation.mirror_merged_prs if pr.eligibility_mode]
    ungated_scores = [pr.earned_score for pr in evaluation.mirror_merged_prs if not pr.eligibility_mode]

    assert evaluation.is_eligible is False
    assert evaluation.total_score == 7.0
    assert gated_scores == [0.0, 0.0, 0.0, 0.0]
    assert ungated_scores == [7.0]
