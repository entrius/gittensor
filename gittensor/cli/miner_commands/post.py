# Entrius 2025

"""gitt miner post — Broadcast GitHub PAT to validators."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass

import click
import requests
from rich.table import Table

from gittensor.cli.miner_commands.helpers import (
    DEFAULT_MIN_VALIDATOR_STAKE,
    DEFAULT_MIN_VALIDATOR_VTRUST,
    NETUID_DEFAULT,
    _connect_bittensor,
    _error,
    _load_config_value,
    _pat_post_aggregate_counts,
    _pat_post_row_category,
    _print,
    _render_skipped_validators,
    _require_registered,
    _require_validator_axons,
    _resolve_endpoint,
    _status,
    console,
    err_console,
)
from gittensor.constants import BASE_GITHUB_API_URL, GITHUB_HTTP_TIMEOUT_SECONDS, GRAPHQL_VIEWER_QUERY
from gittensor.utils.github_api_tools import _is_rate_limited_response, make_graphql_headers, make_headers

_INVALID_PAT_MESSAGE = 'GitHub PAT is invalid or expired. Check your GITTENSOR_MINER_PAT.'

_PAT_POST_STATUS_MARKUP = {
    'accepted': '[green]✓[/green]',
    'rejected': '[red]✗[/red]',
    'no_response': '[yellow]—[/yellow]',
}


@dataclass(frozen=True)
class PatValidationResult:
    is_valid: bool
    github_login: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def valid(cls, github_login: str | None = None) -> 'PatValidationResult':
        return cls(is_valid=True, github_login=github_login)

    @classmethod
    def failed(cls, error_code: str | None, error_message: str) -> 'PatValidationResult':
        return cls(is_valid=False, error_code=error_code, error_message=error_message)


@click.command()
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--pat',
    default=None,
    help='GitHub Personal Access Token. If not provided, falls back to GITTENSOR_MINER_PAT env var or interactive prompt.',
)
@click.option(
    '--min-vtrust',
    type=float,
    default=DEFAULT_MIN_VALIDATOR_VTRUST,
    show_default=True,
    help='Minimum validator_trust to broadcast to.',
)
@click.option(
    '--min-stake',
    type=float,
    default=DEFAULT_MIN_VALIDATOR_STAKE,
    show_default=True,
    help='Minimum validator stake (α) to broadcast to.',
)
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_post(wallet_name, wallet_hotkey, netuid, network, rpc_url, pat, min_vtrust, min_stake, json_mode):
    """Broadcast your GitHub PAT to all validators on the network.

    Validators will validate your PAT (test GitHub API access, check account age),
    then store it locally for use during scoring rounds.

    \b
    PAT resolution order:
        1. --pat flag
        2. GITTENSOR_MINER_PAT environment variable
        3. Interactive prompt (non-JSON mode only)

    \b
    Examples:
        gitt miner post --wallet alice --hotkey default --pat ghp_xxxx
        gitt miner post --wallet alice --hotkey default
        gitt miner post --wallet alice --hotkey default --network test
    """
    from gittensor.synapses import PatBroadcastSynapse

    # 1. Load and validate PAT locally (flag > env var > interactive prompt)
    pat = pat or os.environ.get('GITTENSOR_MINER_PAT')
    if not pat:
        if json_mode:
            _error('--pat flag or GITTENSOR_MINER_PAT environment variable is required for JSON mode.', json_mode)
            sys.exit(1)
        pat = click.prompt('Enter your GitHub Personal Access Token', hide_input=True)

    # 1b. Validate PAT locally
    with _status('[bold]Validating PAT...'):
        pat_validation = _validate_pat_locally(pat)

    if not pat_validation.is_valid:
        _error(
            pat_validation.error_message or _INVALID_PAT_MESSAGE,
            json_mode,
            error_code=pat_validation.error_code,
        )
        sys.exit(1)

    github_login = pat_validation.github_login
    _print(f'[green]PAT is valid.[/green] GitHub account: [bold]@{github_login}[/bold]')

    # 2. Resolve wallet and network
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    _print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]')

    # 3. Set up bittensor objects
    with _status('[bold]Connecting to network...'):
        try:
            wallet, subtensor, metagraph, dendrite = _connect_bittensor(wallet_name, wallet_hotkey, ws_endpoint, netuid)
        except Exception as e:
            _error(f'Failed to initialize bittensor: {e}', json_mode)
            sys.exit(1)

    # Verify miner is registered
    _require_registered(wallet, metagraph, netuid, json_mode)

    # 4. Find active validator axons (vtrust + serving + stake threshold)
    validator_axons, validator_uids, excluded = _require_validator_axons(
        metagraph, json_mode, min_vtrust=min_vtrust, min_stake=min_stake
    )

    # 5. Broadcast
    synapse = PatBroadcastSynapse(github_access_token=pat)

    async def _broadcast():
        return await dendrite(
            axons=validator_axons,
            synapse=synapse,
            deserialize=False,
            timeout=30.0,
        )

    with _status(f'[bold]Broadcasting to {len(validator_axons)} validators...'):
        responses = asyncio.run(_broadcast())

    # 6. Collect results
    results = []
    for uid, axon, resp in zip(validator_uids, validator_axons, responses):
        accepted = getattr(resp, 'accepted', None)
        reason = getattr(resp, 'rejection_reason', None)
        status_code = getattr(resp.dendrite, 'status_code', None) if hasattr(resp, 'dendrite') else None
        results.append(
            {
                'uid': uid,
                'hotkey': axon.hotkey[:16] + '...',
                'accepted': accepted,
                'rejection_reason': reason,
                'status_code': status_code,
            }
        )

    counts = _pat_post_aggregate_counts(results)
    accepted_count = counts['accepted']

    # 7. Display results
    if json_mode:
        click.echo(
            json.dumps(
                {
                    'success': accepted_count > 0,
                    'github_login': github_login,
                    'total_validators': len(results),
                    **counts,
                    'skipped': excluded,
                    'results': results,
                },
                indent=2,
            )
        )
    else:
        table = Table(title='PAT Broadcast Results')
        table.add_column('UID', style='cyan', justify='right')
        table.add_column('Validator', style='dim')
        table.add_column('Status', justify='center')
        table.add_column('Reason', style='dim')

        for r in results:
            category = _pat_post_row_category(r)
            status = _PAT_POST_STATUS_MARKUP[category]
            table.add_row(str(r['uid']), r['hotkey'], status, r.get('rejection_reason') or '')

        console.print(table)
        console.print(f'\n[bold]{accepted_count}/{len(results)} validators accepted your PAT.[/bold]')
        _render_skipped_validators(excluded, json_mode)


def _validate_pat_locally(pat: str) -> PatValidationResult:
    """Validate PAT mirrors the validator-side checks: user identity + GraphQL access."""
    try:
        # Check basic auth and extract login
        user_resp = requests.get(
            f'{BASE_GITHUB_API_URL}/user', headers=make_headers(pat), timeout=GITHUB_HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as e:
        return PatValidationResult.failed(
            'github_network_error',
            f'Failed to reach GitHub API: {e}. Check your network and retry.',
        )

    if user_resp.status_code != 200:
        return _classify_user_lookup_failure(user_resp)

    login: str | None = user_resp.json().get('login') or None

    try:
        # Check GraphQL access (same test the validator runs during PAT broadcast)
        gql_resp = requests.post(
            f'{BASE_GITHUB_API_URL}/graphql',
            json={'query': GRAPHQL_VIEWER_QUERY},
            headers=make_graphql_headers(pat),
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if gql_resp.status_code != 200:
            err_console.print(
                '[red]PAT lacks GraphQL API access. Fine-grained PATs need "Public Repositories (read-only)" permission.[/red]'
            )
            return PatValidationResult.failed(None, _INVALID_PAT_MESSAGE)

        return PatValidationResult.valid(github_login=login)
    except requests.RequestException:
        return PatValidationResult.failed(None, _INVALID_PAT_MESSAGE)


def _classify_user_lookup_failure(response: requests.Response) -> PatValidationResult:
    status_code = response.status_code
    if _is_rate_limited_response(response):
        return PatValidationResult.failed(
            'github_rate_limited',
            'GitHub API rate limit reached. Retry later.',
        )
    if status_code == 408 or 500 <= status_code <= 599:
        return PatValidationResult.failed(
            'github_unavailable',
            f'GitHub API is currently unavailable (HTTP {status_code}). Retry in a few minutes.',
        )
    return PatValidationResult.failed('pat_invalid', _INVALID_PAT_MESSAGE)
