from unittest.mock import Mock

import pytest

classes = pytest.importorskip('gittensor.classes')
mirror_combine = pytest.importorskip('gittensor.validator.oss_contributions.mirror.combine')
mirror_client_module = pytest.importorskip('gittensor.utils.mirror.client')
mirror_eval_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.evaluation')
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
mirror_scoring = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scoring')
reward_module = pytest.importorskip('gittensor.validator.oss_contributions.normalize')
scoring_module = pytest.importorskip('gittensor.validator.oss_contributions.scoring')
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

MinerEvaluation = classes.MinerEvaluation
MirrorFile = mirror_models.MirrorFile
MirrorMinerEvaluation = mirror_eval_module.MirrorMinerEvaluation
MirrorPullRequest = mirror_models.MirrorPullRequest
MirrorRequestError = mirror_client_module.MirrorRequestError
RepositoryConfig = load_weights.RepositoryConfig
ScoredMirrorPR = scored_pr_module.ScoredMirrorPR
combine = mirror_combine.combine
finalize_miner_scores = scoring_module.finalize_miner_scores
normalize_rewards_linear = reward_module.normalize_rewards_linear
score_mirror_miner_prs = mirror_scoring.score_mirror_miner_prs


def _pr(
    pr_number: int,
    github_id: str,
    repo: str,
    state: str = 'MERGED',
    scoring_data_stored: bool = True,
) -> MirrorPullRequest:
    return MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': pr_number,
            'title': f'PR {pr_number}',
            'body': 'b',
            'state': state,
            'author_github_id': github_id,
            'author_login': f'user-{github_id}',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-15T00:00:00Z',
            'closed_at': '2026-04-18T10:00:00Z' if state in ('CLOSED', 'MERGED') else None,
            'merged_at': '2026-04-18T10:00:00Z' if state == 'MERGED' else None,
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': 1.0 if state == 'MERGED' else None,
            'merged_by_login': 'maintainer' if state == 'MERGED' else None,
            'base_ref': 'main',
            'head_ref': f'feature/{pr_number}',
            'head_repo_full_name': repo,
            'default_branch': 'main',
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 1,
            'deletions': 0,
            'commits_count': 1,
            'scoring_data_stored': scoring_data_stored,
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


def _file() -> MirrorFile:
    return MirrorFile.from_dict(
        {
            'filename': 'src/code.py',
            'previous_filename': None,
            'status': 'modified',
            'additions': 1,
            'deletions': 0,
            'changes': 1,
            'is_binary': False,
            'head_content': 'x = 2\n',
            'base_content': 'x = 1\n',
        }
    )


def _mirror_eval(
    uid: int,
    github_id: str,
    repo: str,
    scored_merged: int,
    unavailable_merged: int = 0,
    closed: int = 0,
) -> MirrorMinerEvaluation:
    eval_ = MirrorMinerEvaluation(uid=uid, hotkey=f'hk{uid}', github_id=github_id)
    eval_.merged_prs = [
        ScoredMirrorPR(pr=_pr(i, github_id, repo, scoring_data_stored=True)) for i in range(1, scored_merged + 1)
    ]
    eval_.merged_prs.extend(
        ScoredMirrorPR(pr=_pr(i, github_id, repo, scoring_data_stored=False))
        for i in range(scored_merged + 1, scored_merged + unavailable_merged + 1)
    )
    eval_.closed_prs = [
        ScoredMirrorPR(pr=_pr(100 + i, github_id, repo, state='CLOSED', scoring_data_stored=False))
        for i in range(closed)
    ]
    return eval_


def test_unscored_mirror_merged_prs_do_not_rescue_credibility_gate(monkeypatch):
    def fake_base_score(scored, _file_changes, _file_contents, _programming_languages, _token_config):
        scored.token_score = 10.0
        return 100.0

    monkeypatch.setattr(mirror_scoring, '_calculate_base_score', fake_base_score)

    client = Mock()
    client.get_pr_files.return_value = Mock(files=[_file()])

    mirror_repos = {
        'entrius/gittensor-ui': RepositoryConfig(weight=0.5, mirror_enabled=True),
        'entrius/gittensor': RepositoryConfig(weight=1.0, mirror_enabled=True),
    }

    affected_mirror = _mirror_eval(
        uid=7,
        github_id='700',
        repo='entrius/gittensor-ui',
        scored_merged=5,
        unavailable_merged=3,
        closed=3,
    )
    control_mirror = _mirror_eval(
        uid=8,
        github_id='800',
        repo='entrius/gittensor',
        scored_merged=5,
    )

    score_mirror_miner_prs(affected_mirror, mirror_repos, {}, Mock(), client=client)
    score_mirror_miner_prs(control_mirror, mirror_repos, {}, Mock(), client=client)

    affected = MinerEvaluation(uid=7, hotkey='hk7', github_id='700')
    control = MinerEvaluation(uid=8, hotkey='hk8', github_id='800')
    combine(affected, affected_mirror)
    combine(control, control_mirror)

    miner_evals = {7: affected, 8: control}
    finalize_miner_scores(miner_evals)
    rewards = normalize_rewards_linear(miner_evals)

    assert affected.is_eligible is False
    assert affected.credibility == pytest.approx(5 / 7)
    assert affected.total_score == 0.0
    assert control.is_eligible is True
    assert rewards[7] == 0.0
    assert rewards[8] == 1.0


@pytest.mark.parametrize('mode', ['scoring_data_unavailable', 'fetch_error', 'empty_files'])
def test_unscored_merged_prs_are_removed_before_finalization(mode):
    mirror_eval = _mirror_eval(
        uid=7,
        github_id='700',
        repo='entrius/gittensor-ui',
        scored_merged=1,
    )
    client = Mock()

    if mode == 'scoring_data_unavailable':
        mirror_eval.merged_prs[0].pr.scoring_data_stored = False
    elif mode == 'fetch_error':
        client.get_pr_files.side_effect = MirrorRequestError('boom')
    else:
        client.get_pr_files.return_value = Mock(files=[])

    score_mirror_miner_prs(
        mirror_eval,
        {'entrius/gittensor-ui': RepositoryConfig(weight=0.5, mirror_enabled=True)},
        {},
        Mock(),
        client=client,
    )

    assert mirror_eval.merged_prs == []
