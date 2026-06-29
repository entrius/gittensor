# Entrius 2025

"""Unit tests for `gitt miner scan` opportunity ranking.

The scoring + ranking helpers are pure and accept injectable fetchers, so these
tests run without any network access.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gittensor.cli.miner_commands.scan import (
    Opportunity,
    _age_days,
    fetch_open_issues,
    freshness_factor,
    gather_opportunities,
    opportunity_score,
)

_NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


def test_freshness_decays_with_age():
    assert freshness_factor(0) == 1.0
    assert abs(freshness_factor(30, half_life=30) - 0.5) < 1e-9
    assert freshness_factor(60, half_life=30) < freshness_factor(30, half_life=30)


def test_opportunity_score_monotonicity():
    base = opportunity_score(emission_share=0.1, multiplier=2.0, age_days=0, competition=0)
    # More emission share -> higher score.
    assert opportunity_score(0.2, 2.0, 0, 0) > base
    # More competition -> lower score.
    assert opportunity_score(0.1, 2.0, 0, 3) < base
    # Older issue -> lower score.
    assert opportunity_score(0.1, 2.0, 90, 0) < base


def test_gather_skips_zero_share_and_ranks_desc():
    repos = {
        'big/repo': SimpleNamespace(emission_share=0.35, scoring=None, label_multipliers={'feature': 3.0}),
        'small/repo': SimpleNamespace(emission_share=0.02, scoring=None, label_multipliers=None),
        'dead/repo': SimpleNamespace(emission_share=0.0, scoring=None, label_multipliers=None),  # skipped
    }

    def fake_fetch(repo, token, limit):
        return [{'number': 1, 'title': f'issue in {repo}', 'html_url': f'https://x/{repo}/1', 'created_at': '2026-06-20T00:00:00Z'}]

    def no_competition(repo, number, token):
        return 0

    opps = gather_opportunities(
        repos,
        'tok',
        issue_fetcher=fake_fetch,
        competition_fn=no_competition,
        now=_NOW,
    )

    repos_seen = [o.repo for o in opps]
    assert 'dead/repo' not in repos_seen  # zero emission share dropped
    assert len(opps) == 2
    assert opps[0].repo == 'big/repo'  # highest share+multiplier ranks first
    assert all(isinstance(o, Opportunity) for o in opps)
    assert opps[0].score >= opps[1].score


def test_competition_only_queried_when_enabled():
    repos = {'a/b': SimpleNamespace(emission_share=0.1, scoring=None, label_multipliers={'feature': 2.0})}
    calls = {'n': 0}

    def fake_fetch(repo, token, limit):
        return [{'number': 9, 'title': 't', 'html_url': 'u', 'created_at': '2026-06-25T00:00:00Z'}]

    def counting_competition(repo, number, token):
        calls['n'] += 1
        return 2

    # Disabled: competition_fn must not be called.
    gather_opportunities(repos, 'tok', issue_fetcher=fake_fetch, competition_fn=counting_competition, now=_NOW)
    assert calls['n'] == 0

    # Enabled: competition_fn is consulted and reflected in the result.
    opps = gather_opportunities(
        repos,
        'tok',
        check_competition=True,
        issue_fetcher=fake_fetch,
        competition_fn=counting_competition,
        now=_NOW,
    )
    assert calls['n'] == 1
    assert opps[0].competition == 2


def test_age_days_handles_naive_timestamp_without_crashing():
    # A parseable but timezone-naive timestamp must not raise (was a TypeError).
    assert _age_days('2026-06-01T00:00:00', now=_NOW) == 28.0  # treated as UTC
    assert _age_days('not-a-date', now=_NOW) == 0.0  # malformed -> 0.0


@patch('gittensor.utils.github_api_tools.get_session')
def test_fetch_open_issues_paginates_and_excludes_prs(mock_get_session):
    def resp(items):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = items
        return r

    page1 = [
        {'number': n, 'title': 't', 'html_url': 'u', 'created_at': '2026-06-01T00:00:00Z'}
        for n in range(100)
    ]
    page1[0]['pull_request'] = {'url': 'x'}  # one PR that must be filtered out
    page2 = [
        {'number': n, 'title': 't', 'html_url': 'u', 'created_at': '2026-06-01T00:00:00Z'}
        for n in range(100, 130)
    ]
    session = MagicMock()
    session.get.side_effect = [resp(page1), resp(page2)]
    mock_get_session.return_value = session

    out = fetch_open_issues('owner/repo', 'tok', 150)

    assert session.get.call_count == 2  # paginated past the 100-per-page cap
    assert all('pull_request' not in i for i in out)  # PRs excluded
    assert len(out) == 129  # 99 issues (page1) + 30 (page2)
