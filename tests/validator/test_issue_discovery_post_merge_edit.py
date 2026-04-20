"""Post-merge body/title edit detection.

Benign activity (bot comments, labels) must not demote solved issues;
real body/title edits after merge must.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from gittensor.classes import Issue, MinerEvaluation, PRState, PullRequest
from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE
from gittensor.validator.issue_discovery.scoring import _collect_issues_from_prs, _DiscovererData
from gittensor.validator.utils.load_weights import RepositoryConfig

DISCOVERER_UID = 1
SOLVER_UID = 2
DISCOVERER_GH = '1001'
SOLVER_GH = '2002'
REPO = 'owner/repo'
MERGED_AT = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


def _make_issue(
    *,
    updated_at: Optional[datetime] = None,
    body_or_title_edited_at: Optional[datetime] = None,
) -> Issue:
    return Issue(
        number=42,
        pr_number=7,
        repository_full_name=REPO,
        title='bug',
        created_at=MERGED_AT - timedelta(days=5),
        closed_at=MERGED_AT,
        author_login='alice',
        state='CLOSED',
        state_reason='COMPLETED',
        author_github_id=DISCOVERER_GH,
        updated_at=updated_at,
        body_or_title_edited_at=body_or_title_edited_at,
    )


def _make_pr(issue: Issue) -> PullRequest:
    return PullRequest(
        number=7,
        repository_full_name=REPO,
        uid=SOLVER_UID,
        hotkey='hk',
        github_id=SOLVER_GH,
        title='fix',
        author_login='bob',
        merged_at=MERGED_AT,
        created_at=MERGED_AT - timedelta(days=1),
        pr_state=PRState.MERGED,
        token_score=float(MIN_TOKEN_SCORE_FOR_BASE_SCORE) + 10.0,
        base_score=10.0,
        issues=[issue],
    )


def _evaluations(pr: PullRequest) -> Dict[int, MinerEvaluation]:
    discoverer = MinerEvaluation(uid=DISCOVERER_UID, hotkey='hk1', github_id=DISCOVERER_GH)
    solver = MinerEvaluation(uid=SOLVER_UID, hotkey='hk2', github_id=SOLVER_GH)
    solver.merged_pull_requests = [pr]
    return {DISCOVERER_UID: discoverer, SOLVER_UID: solver}


def _repos() -> Dict[str, RepositoryConfig]:
    return {REPO: RepositoryConfig(weight=1.0)}


def _run(pr: PullRequest) -> _DiscovererData:
    evaluations = _evaluations(pr)
    gh_to_uid = {DISCOVERER_GH: DISCOVERER_UID, SOLVER_GH: SOLVER_UID}
    discoverer_data: Dict[str, _DiscovererData] = defaultdict(_DiscovererData)
    _collect_issues_from_prs(evaluations, gh_to_uid, discoverer_data, _repos())
    return discoverer_data[DISCOVERER_GH]


def test_benign_updated_at_after_merge_is_ignored():
    """Bot activity bumps updated_at but not body_or_title_edited_at → stays solved."""
    issue = _make_issue(
        updated_at=MERGED_AT + timedelta(hours=1),  # noisy bot bump
        body_or_title_edited_at=None,
    )
    pr = _make_pr(issue)
    data = _run(pr)

    assert data.solved_count == 1
    assert data.closed_count == 0
    assert len(data.scored_issues) == 1


def test_real_body_edit_after_merge_demotes():
    """An actual body edit after merge demotes solved → closed."""
    issue = _make_issue(
        updated_at=MERGED_AT + timedelta(hours=1),
        body_or_title_edited_at=MERGED_AT + timedelta(hours=1),
    )
    pr = _make_pr(issue)
    data = _run(pr)

    assert data.solved_count == 0
    assert data.closed_count == 1
    assert data.scored_issues == []


def test_edit_before_merge_is_ignored():
    """Body edits prior to merge are fine."""
    issue = _make_issue(
        body_or_title_edited_at=MERGED_AT - timedelta(hours=1),
    )
    pr = _make_pr(issue)
    data = _run(pr)

    assert data.solved_count == 1
    assert data.closed_count == 0
