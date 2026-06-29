# Entrius 2025

"""Unit tests for `gitt miner advisor` recommendation analysis.

The analyzer is pure (operates on the serialized pipeline payload), so these
tests run without network, chain, or DB access.
"""

from gittensor.cli.miner_commands.advisor import Impact, build_recommendations


def _impacts(recs):
    return {r.impact for r in recs}


def test_failed_evaluation_yields_single_critical():
    payload = {'miner_evaluation': {'failed_reason': 'no PAT'}, 'rewards': {}}
    recs = build_recommendations(payload, thresholds={})
    assert len(recs) == 1
    assert recs[0].impact is Impact.CRITICAL
    assert 'no PAT' in recs[0].detail


def test_ineligible_repo_is_critical_with_numbers():
    payload = {
        'miner_evaluation': {
            'failed_reason': None,
            'total_score': 0.0,
            'unique_repos_count': 1,
            'total_collateral_score': 0.0,
            'repo_evaluations': {
                'owner/repo': {'is_eligible': False, 'credibility': 0.4, 'total_merged_prs': 1},
            },
            'merged_pull_requests': [],
        },
        'rewards': {'blended_final': 0.0},
    }
    thresholds = {'owner/repo': {'min_valid_merged_prs': 3, 'min_credibility': 0.6}}
    recs = build_recommendations(payload, thresholds)
    critical = [r for r in recs if r.impact is Impact.CRITICAL]
    assert critical and critical[0].repo == 'owner/repo'
    assert '1/3' in critical[0].detail  # merged-PR gap surfaced
    assert '0.6' in critical[0].detail  # credibility threshold surfaced


def test_reductions_and_tips_detected():
    payload = {
        'miner_evaluation': {
            'failed_reason': None,
            'total_score': 12.0,
            'unique_repos_count': 1,
            'total_collateral_score': 2.5,
            'repo_evaluations': {'owner/repo': {'is_eligible': True, 'credibility': 0.9, 'total_merged_prs': 5}},
            'merged_pull_requests': [
                {
                    'repository_full_name': 'owner/repo',
                    'number': 7,
                    'review_quality_multiplier': 0.5,  # WARNING: review penalty
                    'open_pr_spam_multiplier': 1.0,
                    'time_decay_multiplier': 1.0,
                    'issue_multiplier': 1.0,  # TIP: no linked issue
                    'label_multiplier': 1.0,
                },
            ],
        },
        'rewards': {'blended_final': 0.01},
    }
    recs = build_recommendations(payload, thresholds={})
    impacts = _impacts(recs)
    assert Impact.WARNING in impacts  # review penalty + collateral
    assert Impact.TIP in impacts  # missing issue link
    assert Impact.INFO in impacts  # standing summary


def test_recommendations_sorted_by_impact():
    payload = {
        'miner_evaluation': {
            'failed_reason': None,
            'total_score': 1.0,
            'unique_repos_count': 1,
            'total_collateral_score': 1.0,
            'repo_evaluations': {'a/b': {'is_eligible': False, 'credibility': 0.0, 'total_merged_prs': 0}},
            'merged_pull_requests': [],
        },
        'rewards': {'blended_final': 0.0},
    }
    thresholds = {'a/b': {'min_valid_merged_prs': 1, 'min_credibility': 0.5}}
    recs = build_recommendations(payload, thresholds)
    order = [r.impact for r in recs]
    # CRITICAL must come before INFO in the rendered order.
    assert order.index(Impact.CRITICAL) < order.index(Impact.INFO)
