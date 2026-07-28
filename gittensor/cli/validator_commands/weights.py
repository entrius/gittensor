# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Validator repo weight basket commands (repos-v0 contract `set_basket`).

Commands:
    gitt validator weights show
    gitt validator weights set
    gitt validator weights clear
"""

from typing import Dict, List, Optional, Tuple

import click
from rich.table import Table

from gittensor.cli.core.help import StyledCommand, StyledGroup
from gittensor.cli.core.helpers import (
    apply_click_options,
    confirm_or_abort,
    console,
    err_console,
    handle_exception,
    loading_context,
    print_network_header,
    print_success,
    with_cli_behavior_options,
    with_network_contract_options,
    with_wallet_options,
)
from gittensor.cli.core.json_output import emit_json
from gittensor.cli.repo_commands.helpers import (
    DEFAULT_BASKET_CAP,
    WEIGHT_SUM,
    make_registry_wallet_client,
    resolve_repos_contract_and_network,
)

CONTRACT_HELP = 'Repos contract address (uses config if empty)'


def _weights_options(func):
    """Standard option bundle for basket commands."""
    return apply_click_options(
        with_wallet_options(),
        with_network_contract_options(CONTRACT_HELP),
        with_cli_behavior_options(include_json=True, include_yes=True),
    )(func)


def quantize_weights(weights: Dict[int, float]) -> List[Tuple[int, int]]:
    """Quantize relative weights to u16 entries summing exactly WEIGHT_SUM.

    Largest-remainder method mirroring codec.canonicalize_prefs; deterministic
    tie-breaks by github id. Entries that quantize to zero are dropped.
    """
    total = sum(weights.values())
    floors = {gid: int(w * WEIGHT_SUM // total) for gid, w in weights.items()}
    remainders = sorted(weights, key=lambda gid: (-(weights[gid] * WEIGHT_SUM / total - floors[gid]), gid))
    for gid in remainders[: WEIGHT_SUM - sum(floors.values())]:
        floors[gid] += 1
    return sorted((gid, w) for gid, w in floors.items() if w > 0)


def _parse_weight_pairs(pairs: Tuple[str, ...]) -> Dict[str, float]:
    """Parse repo=weight pairs into {ref: weight}. Refs stay unresolved strings."""
    parsed: Dict[str, float] = {}
    for pair in pairs:
        ref, sep, raw = pair.partition('=')
        ref = ref.strip().lower()
        if not sep or not ref:
            raise click.BadParameter(f'Expected repo=weight (got {pair!r})', param_hint='WEIGHTS')
        try:
            weight = float(raw)
        except ValueError:
            raise click.BadParameter(f'Invalid weight for {ref}: {raw!r}', param_hint='WEIGHTS')
        if weight <= 0:
            raise click.BadParameter(f'Weight for {ref} must be positive (got {raw})', param_hint='WEIGHTS')
        if ref in parsed:
            raise click.BadParameter(f'Duplicate repo {ref!r}', param_hint='WEIGHTS')
        parsed[ref] = weight
    if not parsed:
        raise click.BadParameter('No repo=weight pairs given', param_hint='WEIGHTS')
    return parsed


def _resolve_refs(prefs: Dict[str, float], repos) -> Dict[int, float]:
    """Resolve id-or-name refs against the active registry. Raises on unknowns."""
    by_name = {repo.full_name: repo.github_id for repo in repos}
    ids = set(by_name.values())
    resolved: Dict[int, float] = {}
    unknown = []
    for ref, weight in prefs.items():
        gid = int(ref) if ref.isdigit() else by_name.get(ref)
        if gid is None or gid not in ids:
            unknown.append(ref)
            continue
        if gid in resolved:
            raise click.ClickException(f'Repo {ref!r} given twice (as id and name)')
        resolved[gid] = weight
    if unknown:
        raise click.ClickException(f'Not registered or inactive: {", ".join(sorted(unknown))}')
    return resolved


def _basket_table(title: str, entries: List[Tuple[int, int]], names: Dict[int, str]) -> Table:
    table = Table(title=title, show_header=True, header_style='bold magenta')
    table.add_column('Repository', style='cyan')
    table.add_column('Weight', justify='right')
    table.add_column('Share', justify='right')
    for gid, weight in sorted(entries, key=lambda e: -e[1]):
        table.add_row(names.get(gid, f'id {gid}'), str(weight), f'{weight / WEIGHT_SUM:.1%}')
    return table


def _basket_json(entries: List[Tuple[int, int]], names: Dict[int, str]) -> List[dict]:
    return [
        {'github_id': gid, 'repo': names.get(gid), 'weight': weight, 'share': round(weight / WEIGHT_SUM, 6)}
        for gid, weight in sorted(entries)
    ]


@click.group(name='weights', cls=StyledGroup)
def weights_group():
    """Repository weight basket voting.

    Whitelisted validators publish a basket of up to 10 registered repos with
    relative weights to the repos contract. The stake-weighted mean of all
    baskets becomes the repository emission shares at each snapshot.
    """
    pass


@weights_group.command('show', cls=StyledCommand)
@_weights_options
def weights_show(
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Show your basket and all published validator baskets.

    [dim]Examples:
        $ gitt validator weights show
        $ gitt v weights show --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)

    try:
        with loading_context('Reading baskets...', as_json):
            wallet, subtensor, client = make_registry_wallet_client(
                contract_addr, ws_endpoint, wallet_name, wallet_hotkey
            )
            at = subtensor.substrate.get_chain_head()
            names = {repo.github_id: repo.full_name for repo in client.get_all_repos(at=at)}
            baskets = client.get_all_baskets(at=at)
            try:
                own_hotkey: Optional[str] = wallet.hotkey.ss58_address
                own = client.get_basket(own_hotkey, at=at)
            except Exception:
                own_hotkey, own = None, None
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e), error_type='read_failed')

    if as_json:
        emit_json(
            {
                'success': True,
                'hotkey': own_hotkey,
                'own_basket': _basket_json(own, names) if own else None,
                'baskets': {hotkey: _basket_json(entries, names) for hotkey, entries in sorted(baskets.items())},
            }
        )
        return

    if own_hotkey is None:
        err_console.print('[yellow]Could not load wallet hotkey — showing all baskets only.[/yellow]')
    elif own:
        console.print(_basket_table(f'Your basket ({own_hotkey})', own, names))
    else:
        err_console.print(f'[yellow]No basket on chain for {own_hotkey}.[/yellow]')

    others = {hotkey: entries for hotkey, entries in baskets.items() if hotkey != own_hotkey}
    if others:
        for hotkey, entries in sorted(others.items()):
            console.print(_basket_table(hotkey, entries, names))
    else:
        err_console.print('[dim]No other baskets published.[/dim]')


@weights_group.command('set', cls=StyledCommand)
@click.argument('weights', type=str, nargs=-1, required=True)
@_weights_options
def weights_set(
    weights: Tuple[str, ...],
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Publish your weight basket as repo=weight pairs (whitelisted hotkey signs).

    [dim]Repos are owner/name or GitHub numeric ids and must be registered and
    active. Weights are relative — they are normalized to u16 integers summing
    65535 before publishing.[/dim]

    [dim]Examples:
        $ gitt validator weights set entrius/gittensor=60 latent-to/btcli=40
        $ gitt v weights set 987654321=1 --json -y
    [/dim]
    """
    prefs = _parse_weight_pairs(weights)
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)

    try:
        with loading_context('Validating against the registry...', as_json):
            wallet, subtensor, client = make_registry_wallet_client(
                contract_addr, ws_endpoint, wallet_name, wallet_hotkey
            )
            at = subtensor.substrate.get_chain_head()
            repos = client.get_all_repos(at=at)
            resolved = _resolve_refs(prefs, repos)
            packed = client.get_registry(at=at)
            cap = packed.constants.basket_cap if packed else DEFAULT_BASKET_CAP
    except click.ClickException as e:
        handle_exception(as_json=as_json, message=str(e), error_type='bad_parameter')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if len(resolved) > cap:
        handle_exception(
            as_json=as_json,
            message=f'Basket is capped at {cap} repos (got {len(resolved)})',
            error_type='bad_parameter',
        )

    entries = quantize_weights(resolved)
    names = {repo.github_id: repo.full_name for repo in repos}
    dropped = sorted(names[gid] for gid in resolved if gid not in {e[0] for e in entries})
    if dropped and not as_json:
        err_console.print(f'[yellow]Dropped (weight quantized to zero): {", ".join(dropped)}[/yellow]')

    err_console.print(_basket_table(f'Basket to publish ({wallet.hotkey.ss58_address})', entries, names))
    if not confirm_or_abort('Publish this basket?', yes, default=True):
        return

    try:
        with loading_context('Publishing basket...', as_json):
            published = client.set_basket(entries, wallet)
    except ValueError as e:
        handle_exception(as_json=as_json, message=str(e), error_type='bad_parameter')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if not published:
        handle_exception(
            as_json=as_json,
            message='set_basket was rejected — check the hotkey is whitelisted and funded.',
            error_type='tx_failed',
        )
    if as_json:
        emit_json({'success': True, 'entries': _basket_json(entries, names), 'dropped': dropped})
    else:
        print_success('Basket published! It takes effect at the next snapshot.')


@weights_group.command('clear', cls=StyledCommand)
@_weights_options
def weights_clear(
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Clear your published basket (whitelisted hotkey signs).

    [dim]Examples:
        $ gitt validator weights clear
        $ gitt v weights clear -y
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)

    try:
        with loading_context('Connecting to network...', as_json):
            wallet, _subtensor, client = make_registry_wallet_client(
                contract_addr, ws_endpoint, wallet_name, wallet_hotkey
            )
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if not confirm_or_abort(f'Clear the basket for {wallet.hotkey.ss58_address}?', yes):
        return

    try:
        with loading_context('Clearing basket...', as_json):
            cleared = client.clear_basket(wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if not cleared:
        handle_exception(as_json=as_json, message='clear_basket was rejected by the chain.', error_type='tx_failed')
    if as_json:
        emit_json({'success': True, 'cleared': True})
    else:
        print_success('Basket cleared.')
