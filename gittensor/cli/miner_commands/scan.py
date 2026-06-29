# Entrius 2025

"""gitt miner scan - Rank open GitHub issues across whitelisted repos by TAO
opportunity.

For every repo in ``master_repositories.json`` with a positive emission share,
fetch its open issues (REST) and order them by an opportunity score combining:

    - emission_share   how much of the subnet pool the repo carries
    - multiplier        the best issue/label multiplier you could realistically earn
    - freshness         newer issues are less likely already solved
    - competition       (optional) fewer existing referencing PRs is better

The scoring helpers are pure and injectable so the ranking is unit-testable
without network access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import click
from rich.console import Console
from rich.table import Table

from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.helpers import _error

console = Console()

# Half the opportunity weight is gone once an issue is this many days old.
FRESHNESS_HALF_LIFE_DAYS = 30.0
DEFAULT_ISSUES_PER_REPO = 10
DEFAULT_TOP = 20
GITHUB_MAX_PER_PAGE = 100  # GitHub REST caps per_page at 100


@dataclass
class Opportunity:
    repo: str
    issue_number: int
    title: str
    url: str
    emission_share: float
    multiplier: float
    age_days: float
    competition: int
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'repo': self.repo,
            'issue_number': self.issue_number,
            'title': self.title,
            'url': self.url,
            'emission_share': round(self.emission_share, 6),
            'multiplier': round(self.multiplier, 3),
            'age_days': round(self.age_days, 1),
            'competition': self.competition,
            'score': round(self.score, 6),
        }


def freshness_factor(age_days: float, half_life: float = FRESHNESS_HALF_LIFE_DAYS) -> float:
    """Exponential decay in [0, 1]: 1.0 at age 0, 0.5 at one half-life."""
    if age_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / max(half_life, 1e-9))


def best_repo_multiplier(repo_config: Any) -> float:
    """The best multiplier a contributor could realistically earn in a repo:
    the issue-link multiplier times the strongest configured label multiplier.
    """
    from gittensor.validator.utils.load_weights import resolve_scoring

    scoring = resolve_scoring(getattr(repo_config, 'scoring', None))
    issue_mult = max(scoring.standard_issue_multiplier, scoring.maintainer_issue_multiplier)
    labels = getattr(repo_config, 'label_multipliers', None) or {}
    best_label = max([float(v) for v in labels.values()], default=1.0)
    # A zero default label means unlabeled PRs score nothing; only a positive
    # configured label earns, so fall back to the best label, not 1.0.
    return issue_mult * max(best_label, 0.0 if best_label else 1.0)


def opportunity_score(emission_share: float, multiplier: float, age_days: float, competition: int) -> float:
    """Higher is better. Linear in share/multiplier/freshness, divided by crowding."""
    return (emission_share * multiplier * freshness_factor(age_days)) / (1 + max(0, competition))


def _age_days(created_at: str, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return 0.0
    # A parseable but timezone-naive value (no 'Z'/offset) would raise TypeError
    # when subtracted from the aware `now`; treat such timestamps as UTC.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds() / 86400.0)


def fetch_open_issues(repo: str, token: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch up to ``limit`` open issues for a repo via REST, excluding PRs.

    GitHub caps ``per_page`` at 100, so a ``limit`` above that is paginated
    rather than silently truncated. GitHub's issues endpoint also returns pull
    requests; items carrying a ``pull_request`` key are filtered out.
    """
    from gittensor.constants import BASE_GITHUB_API_URL, GITHUB_HTTP_TIMEOUT_SECONDS
    from gittensor.utils.github_api_tools import get_session

    session = get_session(token)
    per_page = min(limit, GITHUB_MAX_PER_PAGE)
    issues: List[Dict[str, Any]] = []
    page = 1
    while len(issues) < limit:
        resp = session.get(
            f'{BASE_GITHUB_API_URL}/repos/{repo}/issues',
            params={
                'state': 'open',
                'sort': 'created',
                'direction': 'desc',
                'per_page': per_page,
                'page': page,
            },
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        issues.extend(item for item in batch if 'pull_request' not in item)
        if len(batch) < per_page:  # final page reached
            break
        page += 1
    return issues[:limit]


def _competition_count(repo: str, issue_number: int, token: str) -> int:
    from gittensor.utils.github_api_tools import find_prs_for_issue

    return len(find_prs_for_issue(repo, issue_number, open_only=True, token=token) or [])


def gather_opportunities(
    repositories: Dict[str, Any],
    token: str,
    *,
    issues_per_repo: int = DEFAULT_ISSUES_PER_REPO,
    check_competition: bool = False,
    issue_fetcher: Callable[[str, str, int], List[Dict[str, Any]]] = fetch_open_issues,
    competition_fn: Callable[[str, int, str], int] = _competition_count,
    now: Optional[datetime] = None,
) -> List[Opportunity]:
    """Rank open issues across repos with a positive emission share."""
    opportunities: List[Opportunity] = []
    for repo, cfg in repositories.items():
        share = float(getattr(cfg, 'emission_share', 0.0))
        if share <= 0:
            continue
        multiplier = best_repo_multiplier(cfg)
        for issue in issue_fetcher(repo, token, issues_per_repo):
            number = int(issue.get('number', 0))
            age = _age_days(str(issue.get('created_at', '')), now=now)
            competition = competition_fn(repo, number, token) if check_competition else 0
            opportunities.append(
                Opportunity(
                    repo=repo,
                    issue_number=number,
                    title=str(issue.get('title', '')),
                    url=str(issue.get('html_url', '')),
                    emission_share=share,
                    multiplier=multiplier,
                    age_days=age,
                    competition=competition,
                    score=opportunity_score(share, multiplier, age, competition),
                )
            )
    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities


def _render(opportunities: List[Opportunity]) -> None:
    table = Table(title='Issue opportunities by TAO potential')
    table.add_column('Score', justify='right', style='bold green')
    table.add_column('Repo', style='cyan')
    table.add_column('Issue', justify='right')
    table.add_column('Share', justify='right')
    table.add_column('Mult', justify='right')
    table.add_column('Age (d)', justify='right')
    table.add_column('Comp', justify='right')
    table.add_column('Title', overflow='fold')
    for o in opportunities:
        table.add_row(
            f'{o.score:.5f}',
            o.repo,
            f'#{o.issue_number}',
            f'{o.emission_share:.3f}',
            f'×{o.multiplier:.2f}',
            f'{o.age_days:.0f}',
            str(o.competition),
            o.title,
        )
    console.print(table)


@click.command(name='scan')
@click.option('--pat', default=None, envvar='GITTENSOR_MINER_PAT', help='GitHub PAT. Uses GITTENSOR_MINER_PAT if unset.')
@click.option('--limit', default=DEFAULT_ISSUES_PER_REPO, show_default=True, help='Open issues to pull per repo.')
@click.option('--top', default=DEFAULT_TOP, show_default=True, help='Number of top opportunities to display.')
@click.option(
    '--check-competition',
    is_flag=True,
    default=False,
    help='Count existing referencing PRs per issue (slower; extra API calls).',
)
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Emit ranked opportunities as JSON on stdout.')
def scan_command(pat: Optional[str], limit: int, top: int, check_competition: bool, json_mode: bool) -> None:
    """Rank open issues across whitelisted repos by earning opportunity.

    Example:
        gitt miner scan --pat ghp_xxxxx --check-competition
    """
    if not pat:
        _error('--pat flag or GITTENSOR_MINER_PAT environment variable is required.', json_mode)
        raise SystemExit(1)

    from gittensor.validator.utils.load_weights import load_master_repo_weights

    repositories = load_master_repo_weights()
    if not json_mode:
        console.print('[bold cyan]Scanning whitelisted repos for issue opportunities...[/bold cyan]')

    opportunities = gather_opportunities(
        repositories, pat, issues_per_repo=limit, check_competition=check_competition
    )[:top]

    if json_mode:
        emit_json({'success': True, 'opportunities': [o.to_dict() for o in opportunities]})
    else:
        _render(opportunities)
