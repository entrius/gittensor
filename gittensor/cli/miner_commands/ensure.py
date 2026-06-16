# Entrius 2025

"""gitt miner ensure — keep PAT coverage asserted across validator restarts."""

from __future__ import annotations

import json
import sys
import time

import click
from rich.table import Table

from gittensor.cli.issue_commands.helpers import NETWORK_CHOICE
from gittensor.cli.miner_commands.helpers import (
    DEFAULT_MIN_VALIDATOR_STAKE,
    DEFAULT_MIN_VALIDATOR_VTRUST,
    NETUID_DEFAULT,
    _broadcast_pat_with_retry,
    _connect_bittensor,
    _error,
    _load_config_value,
    _pat_check_row_category,
    _pat_post_row_category,
    _print,
    _probe_pat,
    _render_skipped_validators,
    _require_registered,
    _require_validator_axons,
    _resolve_endpoint,
    _status,
    console,
)
from gittensor.cli.miner_commands.post import _validate_pat_locally

# Probe categories that mean "this validator does not currently hold a usable PAT
# for us" and a re-broadcast should be attempted. 'valid' (has it) and
# 'inconclusive' (has it, but a transient validator-side GitHub check) are left alone.
_MISSING_CATEGORIES = {'no_pat', 'invalid_pat', 'no_response'}

_RESULT_MARKUP = {
    'accepted': '[green]✓ re-sent[/green]',
    'rejected': '[red]✗ rejected[/red]',
    'no_response': '[yellow]— no response[/yellow]',
}


def _ensure_cycle(dendrite, metagraph, *, min_vtrust, min_stake, retries, pat, json_mode):
    """One coverage pass: probe, re-broadcast to validators missing a PAT, report.

    Returns the list of uids still uncovered after the pass. Does not exit.
    """
    validator_axons, validator_uids, excluded = _require_validator_axons(
        metagraph, json_mode, min_vtrust=min_vtrust, min_stake=min_stake
    )
    axon_by_uid = dict(zip(validator_uids, validator_axons))

    with _status(f'[bold]Checking {len(validator_axons)} validators...'):
        probe = _probe_pat(dendrite, validator_axons, validator_uids)
    missing_uids = [r['uid'] for r in probe if _pat_check_row_category(r) in _MISSING_CATEGORIES]
    already_valid = len(validator_uids) - len(missing_uids)

    repost_results: list[dict] = []
    if missing_uids:
        with _status(f'[bold]Re-broadcasting to {len(missing_uids)} validator(s) missing your PAT...'):
            repost_results = _broadcast_pat_with_retry(
                dendrite, [axon_by_uid[u] for u in missing_uids], missing_uids, pat, retries=retries
            )
    still_missing = [r['uid'] for r in repost_results if r.get('accepted') is not True]
    now_valid = already_valid + (len(missing_uids) - len(still_missing))

    if json_mode:
        click.echo(
            json.dumps(
                {
                    'success': len(still_missing) == 0,
                    'total_validators': len(validator_uids),
                    'already_valid': already_valid,
                    'reposted': len(missing_uids),
                    'now_valid': now_valid,
                    'still_missing': still_missing,
                    'results': repost_results,
                    'skipped': excluded,
                },
                indent=2,
            )
        )
    else:
        if not missing_uids:
            console.print(f'[bold green]All {len(validator_uids)} validators already have a valid PAT.[/bold green]')
        else:
            table = Table(title='Re-broadcast (validators that were missing your PAT)')
            table.add_column('UID', style='cyan', justify='right')
            table.add_column('Validator', style='dim')
            table.add_column('Result', justify='center')
            for r in repost_results:
                table.add_row(str(r['uid']), r['hotkey'], _RESULT_MARKUP[_pat_post_row_category(r)])
            console.print(table)
            console.print(f'\n[bold]{now_valid}/{len(validator_uids)} validators now have your PAT.[/bold]')
            if still_missing:
                console.print(f'[yellow]Still uncovered (unreachable this run): {still_missing}[/yellow]')
        _render_skipped_validators(excluded, json_mode)

    return still_missing


@click.command()
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', type=NETWORK_CHOICE, default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--pat',
    default=None,
    envvar='GITTENSOR_MINER_PAT',
    help='GitHub Personal Access Token. Falls back to GITTENSOR_MINER_PAT, then interactive prompt.',
)
@click.option(
    '--min-vtrust',
    type=float,
    default=DEFAULT_MIN_VALIDATOR_VTRUST,
    show_default=True,
    help='Minimum validator_trust to consider.',
)
@click.option(
    '--min-stake',
    type=float,
    default=DEFAULT_MIN_VALIDATOR_STAKE,
    show_default=True,
    help='Minimum validator stake (α) to consider.',
)
@click.option(
    '--retries',
    type=int,
    default=2,
    show_default=True,
    help='Retries for validators that do not respond to the re-broadcast.',
)
@click.option(
    '--watch',
    type=int,
    default=0,
    show_default=True,
    metavar='SECONDS',
    help='Re-run every SECONDS so coverage self-heals without external cron (0 = run once).',
)
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_ensure(
    wallet_name, wallet_hotkey, netuid, network, rpc_url, pat, min_vtrust, min_stake, retries, watch, json_mode
):
    """Ensure every eligible validator has a valid PAT — re-broadcasting only where missing.

    A single `gitt miner post` is one-shot: once a validator loses its stored PAT
    (e.g. it restarts without persistent storage), you are silently scored 0 by it
    until you manually re-post. This command probes coverage and re-broadcasts your
    PAT **only** to validators that are missing it (no PAT is sent to validators that
    already have one), so it is cheap to run on a schedule (cron) and lets coverage
    self-heal. Exits non-zero if any reachable validator is still uncovered afterward.

    \b
    Examples:
        gitt miner ensure --wallet alice --hotkey default
        gitt miner ensure --wallet alice --hotkey default --json   # for cron / monitoring
    """
    if not pat:
        if json_mode:
            _error('--pat flag or GITTENSOR_MINER_PAT environment variable is required for JSON mode.', json_mode)
            sys.exit(1)
        pat = click.prompt('Enter your GitHub Personal Access Token', hide_input=True)

    with _status('[bold]Validating PAT...'):
        github_login = _validate_pat_locally(pat)
    if github_login is None:
        _error('GitHub PAT is invalid or expired. Check your GITTENSOR_MINER_PAT.', json_mode)
        sys.exit(1)
    _print(f'[green]PAT is valid.[/green] GitHub account: [bold]@{github_login}[/bold]')

    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)
    _print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]')

    with _status('[bold]Connecting to network...'):
        try:
            wallet, subtensor, metagraph, dendrite = _connect_bittensor(wallet_name, wallet_hotkey, ws_endpoint, netuid)
        except Exception as e:
            _error(f'Failed to initialize bittensor: {e}', json_mode)
            sys.exit(1)

    _require_registered(wallet, metagraph, netuid, json_mode)

    cycle_kwargs = dict(min_vtrust=min_vtrust, min_stake=min_stake, retries=retries, pat=pat, json_mode=json_mode)

    # Watch mode: re-assert coverage forever, re-syncing the metagraph each round so
    # validators that (re)join the active set are picked up. Coverage self-heals
    # without external cron. Transient failures (e.g. no eligible validators this
    # round) are swallowed so the loop keeps running.
    if watch > 0:
        _print(f'[dim]Watch mode: re-asserting PAT coverage every {watch}s. Ctrl-C to stop.[/dim]')
        try:
            while True:
                try:
                    _ensure_cycle(dendrite, metagraph, **cycle_kwargs)
                except SystemExit:
                    pass
                time.sleep(watch)
                metagraph = subtensor.metagraph(netuid=netuid)
        except KeyboardInterrupt:
            sys.exit(0)

    # One-shot (cron-friendly): non-zero exit if any reachable validator is still uncovered.
    still_missing = _ensure_cycle(dendrite, metagraph, **cycle_kwargs)
    if still_missing:
        sys.exit(1)
