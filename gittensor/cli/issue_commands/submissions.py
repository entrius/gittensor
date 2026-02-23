# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Issue submissions command (`gitt issues submissions`)."""

import click

from .help import StyledCommand
from .helpers import (
    emit_json,
    fetch_issue_from_contract,
    fetch_open_issue_pull_requests,
    get_contract_address,
    handle_exception,
    loading_context,
    print_issue_submission_table,
    print_network_header,
    print_warning,
    resolve_network,
    validate_issue_id,
)


@click.command('submissions', cls=StyledCommand)
@click.option(
    '--id',
    'issue_id',
    required=True,
    type=int,
    help='On-chain issue ID',
)
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses default if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_submissions(
    issue_id: int,
    network: str | None,
    rpc_url: str | None,
    contract: str,
    verbose: bool,
    as_json: bool,
):
    """
    List open PR submissions for a bountied issue.

    [dim]This command shows PRs that reference or target the issue, filtered to open PRs only.[/dim]

    [dim]Examples
        $ gitt issues submissions --id 42
        $ gitt i submissions --id 42
        $ gitt i submissions --id 42 --json
    [/dim]
    """
    try:
        validate_issue_id(issue_id, 'id')
    except click.BadParameter as e:
        handle_exception(as_json, str(e), 'bad_parameter')

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        handle_exception(as_json, 'Contract address not configured.', 'config_error')

    if not as_json:
        print_network_header(network_name, contract_addr)

    try:
        with loading_context('Fetching issue from contract...', as_json):
            issue = fetch_issue_from_contract(
                ws_endpoint, contract_addr, issue_id, require_active=False, verbose=verbose
            )
    except click.ClickException as e:
        handle_exception(as_json, str(e))

    repo_name = str(issue.get('repository_full_name', ''))
    issue_number = int(issue.get('issue_number', 0))

    try:
        pull_requests = fetch_open_issue_pull_requests(
            repository_full_name=repo_name,
            issue_number=issue_number,
            as_json=as_json,
        )
    except click.ClickException as e:
        handle_exception(as_json, str(e), 'click_exception')

    if as_json:
        submissions = [
            {
                'number': pr.get('number'),
                'title': pr.get('title'),
                'author': pr.get('author_login'),
                'state': pr.get('state', 'OPEN'),
                'created_at': pr.get('created_at'),
                'merged_at': pr.get('merged_at'),
                'url': pr.get('url'),
                'review_count': int(pr.get('review_count', 0) or 0),
                'closes_issue': issue_number in (pr.get('closing_numbers') or []),
            }
            for pr in pull_requests
        ]
        payload = {
            'issue_id': issue_id,
            'repository': repo_name,
            'issue_number': issue_number,
            'submission_count': len(submissions),
            'submissions': submissions,
        }
        emit_json(payload)
        return

    issue_url = f'https://github.com/{repo_name}/issues/{issue_number}'

    if not pull_requests:
        print_warning(f'No open submissions available ({issue_url})')
        return

    print_issue_submission_table(
        repository_full_name=repo_name,
        issue_number=issue_number,
        pull_requests=pull_requests,
    )
