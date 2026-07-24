# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Repo weight basket commands for validators.

Commands:
    gitt validator weights show
    gitt validator weights set
    gitt validator weights basket <hotkey>
"""

import json
from typing import Dict, Optional, Tuple

import click
import requests
from rich.table import Table

from gittensor.cli.issue_commands.help import StyledGroup
from gittensor.cli.issue_commands.helpers import (
    confirm_or_abort,
    console,
    err_console,
    resolve_network,
    resolve_wallet_config,
    with_wallet_options,
)
from gittensor.cli.issue_commands.types import SS58
from gittensor.cli.miner_commands.helpers import NETUID_DEFAULT
from gittensor.constants import GITTENSOR_MIRROR_DEFAULT_URL
from gittensor.validator.utils.config import CONSENSUS_MAX_REPOS
from gittensor.validator.weight_consensus.chain import fetch_all_commitments, fetch_own_prefs, publish_payload
from gittensor.validator.weight_consensus.codec import CodecError, canonicalize_prefs, decode_prefs, encode_prefs
from gittensor.validator.weight_consensus.consensus import aggregate_preferences, compute_snapshot_block


def _with_chain_options(command):
    for option in (
        click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True),
        click.option('--network', '-n', default=None, help='Network (finney/test/local)'),
        click.option('--rpc-url', default=None, help='Subtensor RPC endpoint (overrides --network)'),
    ):
        command = option(command)
    return command


def _connect(network: Optional[str], rpc_url: Optional[str]):
    import bittensor as bt

    ws_endpoint, network_name = resolve_network(network, rpc_url)
    err_console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    return bt.Subtensor(network=ws_endpoint)


def _check_repo(repo_full_name: str) -> Tuple[Optional[bool], str, Optional[bool]]:
    """Best-effort guardrail checks: (exists_on_github, description, mirror_tracked).

    None means the check could not run (rate limit / network) — warn, don't block.
    """
    exists, description, tracked = None, '', None
    try:
        response = requests.get(f'https://api.github.com/repos/{repo_full_name}', timeout=10)
        if response.status_code == 200:
            exists, description = True, response.json().get('description') or ''
        elif response.status_code == 404:
            exists = False
    except requests.RequestException:
        pass
    try:
        response = requests.get(f'{GITTENSOR_MIRROR_DEFAULT_URL}/api/v1/repos/{repo_full_name}/maintainers', timeout=10)
        tracked = response.status_code == 200
    except requests.RequestException:
        pass
    return exists, description, tracked


def _basket_table(title: str, prefs: Dict[str, int]) -> Table:
    table = Table(title=title)
    table.add_column('Repository', style='cyan')
    table.add_column('Weight', justify='right')
    table.add_column('Share', justify='right')
    total = sum(prefs.values())
    for repo in sorted(prefs, key=lambda r: prefs[r], reverse=True):
        table.add_row(repo, str(prefs[repo]), f'{prefs[repo] / total:.1%}')
    return table


@click.group(name='weights', cls=StyledGroup)
def weights_group():
    """Repository weight basket voting.

    Validators publish a basket of up to 10 repos with relative weights to the
    chain. The stake-weighted mean of all baskets becomes the repository
    emission shares, recomputed at fixed snapshot blocks (~2x/day).
    """


@weights_group.command('show')
@_with_chain_options
def show_weights(netuid: int, network: Optional[str], rpc_url: Optional[str]):
    """Show the current aggregated repository weights and voter turnout."""
    subtensor = _connect(network, rpc_url)
    block = subtensor.get_current_block()
    snapshot = compute_snapshot_block(block)

    try:
        commitments, stakes_rao, permits = _voter_state(subtensor, netuid, snapshot)
        at_block = snapshot
    except Exception as e:
        err_console.print(f'[yellow]Snapshot {snapshot} state unavailable ({e}); showing live chain state.[/yellow]')
        commitments, stakes_rao, permits = _voter_state(subtensor, netuid, block)
        at_block = block

    result = aggregate_preferences(commitments, stakes_rao, permits)
    voters_by_repo: Dict[str, int] = {}
    for hotkey, payload in commitments.items():
        prefs = decode_prefs(payload)
        if prefs and permits.get(hotkey):
            for repo in prefs:
                voters_by_repo[repo] = voters_by_repo.get(repo, 0) + 1

    gate = (
        '[green]ACTIVE[/green]' if result.shares is not None else '[yellow]INACTIVE — baked-in weights apply[/yellow]'
    )
    console.print(f'Snapshot block: {at_block}   Voters: {result.voter_count}   Aggregate: {gate}')
    if result.eligible_stake_rao:
        console.print(
            f'Voting stake: {result.valid_stake_rao / result.eligible_stake_rao:.1%} of eligible (gate needs ≥50%)'
        )

    shares = result.shares or {}
    if not shares:
        return
    table = Table(title='Aggregated repository weights')
    table.add_column('Repository', style='cyan')
    table.add_column('Share', justify='right')
    table.add_column('Voters', justify='right')
    for repo in sorted(shares, key=lambda r: shares[r], reverse=True):
        table.add_row(repo, f'{shares[repo]:.2%}', str(voters_by_repo.get(repo, 0)))
    console.print(table)


@weights_group.command('basket')
@click.argument('hotkey', type=SS58)
@_with_chain_options
def show_basket(hotkey: str, netuid: int, network: Optional[str], rpc_url: Optional[str]):
    """Show a validator's published weight basket."""
    subtensor = _connect(network, rpc_url)
    prefs = fetch_own_prefs(subtensor, netuid, hotkey)
    if prefs is None:
        console.print(f'[yellow]No weight basket on chain for {hotkey}[/yellow]')
        return
    console.print(_basket_table(f'Basket of {hotkey}', prefs))


@weights_group.command('set')
@with_wallet_options()
@_with_chain_options
@click.option(
    '--save-prefs',
    type=click.Path(),
    default=None,
    help='Also write the basket to a prefs JSON (use on the validator host to pin the vote).',
)
@click.option('--yes', is_flag=True, help='Skip confirmation prompt.')
def set_weights(
    wallet_name: str,
    wallet_hotkey: str,
    netuid: int,
    network: Optional[str],
    rpc_url: Optional[str],
    save_prefs: Optional[str],
    yes: bool,
):
    """Interactively build and publish your repo weight basket.

    Any GitHub repository can be voted, max 10 per basket. Weights are
    relative — they are normalized against the rest of your basket. The vote
    takes effect at the next consensus snapshot (~12h cadence).
    """
    import bittensor as bt

    effective_wallet, effective_hotkey = resolve_wallet_config(wallet_name, wallet_hotkey)
    wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
    subtensor = _connect(network, rpc_url)

    basket: Dict[str, float] = {}
    current = fetch_own_prefs(subtensor, netuid, wallet.hotkey.ss58_address)
    if current:
        basket = dict(current)
        console.print(_basket_table('Current on-chain basket', current))
    else:
        console.print('[yellow]No basket on chain yet — starting empty.[/yellow]')

    console.print(
        f'\nEnter [cyan]owner/repo weight[/cyan] to add or update (e.g. [dim]entrius/gittensor 40[/dim]), '
        f'[cyan]-owner/repo[/cyan] to remove, blank line to finish. Max {CONSENSUS_MAX_REPOS} repos.\n'
    )
    while True:
        entry = click.prompt('>', default='', show_default=False).strip()
        if not entry:
            break
        if entry.startswith('-'):
            removed = basket.pop(entry[1:].lower(), None)
            console.print('[dim]removed[/dim]' if removed is not None else '[yellow]not in basket[/yellow]')
            continue
        try:
            name, weight_str = entry.rsplit(None, 1)
            name, weight = name.lower(), float(weight_str)
            if weight <= 0:
                raise ValueError('weight must be positive')
        except ValueError as e:
            console.print(f'[red]Invalid entry ({e})[/red]')
            continue
        if name not in basket and len(basket) >= CONSENSUS_MAX_REPOS:
            console.print(f'[red]Basket is capped at {CONSENSUS_MAX_REPOS} repos — remove one first.[/red]')
            continue

        exists, description, tracked = _check_repo(name)
        if exists is False:
            console.print(f'[red]{name} does not exist on GitHub — not added.[/red]')
            continue
        if description:
            console.print(f'[dim]{description}[/dim]')
        if tracked is False:
            console.print(
                f'[yellow]Warning: {name} is not tracked by the GT mirror — it cannot be scored '
                f'until its owner installs the Gittensor GitHub App and it is registered. '
                f'Until then its emission share redistributes to active repos.[/yellow]'
            )
        basket[name] = weight
        console.print(_basket_table('Draft basket', canonicalize_prefs(basket)))

    if not basket:
        console.print('[yellow]Empty basket — nothing to publish.[/yellow]')
        return

    try:
        prefs = canonicalize_prefs(basket)
        payload = encode_prefs(prefs)
    except CodecError as e:
        console.print(f'[red]Cannot encode basket: {e}[/red]')
        raise SystemExit(1)

    console.print(_basket_table('Final basket', prefs))
    if prefs == current:
        console.print('[green]Identical basket already on chain — nothing to do.[/green]')
        return
    if not confirm_or_abort('Publish this basket to the chain?', yes):
        return

    if publish_payload(subtensor, wallet, netuid, payload):
        console.print('[green]Basket published. It takes effect at the next consensus snapshot (~12h cadence).[/green]')
    else:
        console.print('[red]Publish was rejected by the chain — check hotkey registration and try again.[/red]')
        raise SystemExit(1)

    if save_prefs:
        with open(save_prefs, 'w') as f:
            json.dump({'version': 1, 'repos': basket}, f, indent=2)
        console.print(f'[dim]Prefs written to {save_prefs}[/dim]')


def _voter_state(subtensor, netuid: int, block: int):
    commitments = fetch_all_commitments(subtensor, netuid, block)
    metagraph = subtensor.metagraph(netuid, block=block, lite=True)
    stakes_rao = {hk: int(round(float(metagraph.S[uid]) * 1e9)) for uid, hk in enumerate(metagraph.hotkeys)}
    permits = {hk: bool(metagraph.validator_permit[uid]) for uid, hk in enumerate(metagraph.hotkeys)}
    return commitments, stakes_rao, permits
