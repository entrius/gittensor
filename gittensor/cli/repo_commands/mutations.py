# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Repo registry mutation commands (registrant coldkey signs).

Commands:
    gitt repo register
    gitt repo set-params
    gitt repo set-label / remove-label
    gitt repo set-branch-patterns
    gitt repo update-name
    gitt repo transfer
    gitt repo deregister
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

import click
from rich.panel import Panel

from gittensor.cli.core.help import StyledCommand
from gittensor.cli.core.helpers import (
    apply_click_options,
    confirm_or_abort,
    err_console,
    format_alpha,
    handle_exception,
    loading_context,
    print_network_header,
    with_cli_behavior_options,
    with_network_contract_options,
    with_wallet_options,
)
from gittensor.cli.core.json_output import emit_json
from gittensor.cli.core.types import REPO, SS58

from .helpers import (
    FP6,
    PARAM_KEYS,
    emit_tx_result,
    format_param_value,
    make_registry_wallet_client,
    param_table_hint,
    parse_param_value,
    resolve_github_repo_id,
    resolve_repo_ref,
    resolve_repos_contract_and_network,
)

CONTRACT_HELP = 'Repos contract address (uses config if empty)'

LABEL_MULT_MIN = Decimal('0.5')
LABEL_MULT_MAX = Decimal('2')
MAX_PATTERNS = 4
BRANCH_PATTERN_RE = re.compile(r'^[a-z0-9][a-z0-9._/-]{0,38}\*?$')


def _tx_options(func):
    """Standard option bundle for registry mutations."""
    return apply_click_options(
        with_wallet_options(),
        with_network_contract_options(CONTRACT_HELP),
        with_cli_behavior_options(include_json=True, include_yes=True),
    )(func)


def _connect(contract: str, network: str, rpc_url: str, wallet_name: str, wallet_hotkey: str, as_json: bool):
    """Resolve config, print header, and connect. Returns (wallet, subtensor, client)."""
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)
    with loading_context('Connecting to network...', as_json):
        return make_registry_wallet_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)


@click.command('register', cls=StyledCommand)
@click.argument('repo', type=REPO)
@click.option('--id', 'github_id', type=int, default=None, help='GitHub numeric repo id (skips GitHub lookup)')
@click.option('--fee-hotkey', type=SS58, default=None, help='Hotkey to pull the alpha fee from (default: own hotkey)')
@_tx_options
def repo_register(
    repo: str,
    github_id: Optional[int],
    fee_hotkey: Optional[str],
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Register a repository, paying the dynamic alpha fee.

    [dim]Resolves REPO (owner/name) to its GitHub numeric id, shows the current
    registration price, and submits `register` signed by your coldkey. The fee
    is pulled from your stake on --fee-hotkey and recycled.[/dim]

    [dim]Examples:
        $ gitt repo register entrius/gittensor
        $ gitt r register owner/name --id 987654321 --fee-hotkey 5Hxxx... -y
    [/dim]
    """
    full_name = repo.lower()
    if github_id is None:
        github_id = resolve_github_repo_id(full_name)
    elif github_id <= 0:
        raise click.BadParameter('GitHub id must be positive', param_hint='--id')

    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        fee_hotkey = fee_hotkey or wallet.hotkey.ss58_address
        with loading_context('Quoting registration price...', as_json):
            quote = client.quote_price()
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    price_text = f'{format_alpha(quote, 4)} ALPHA' if quote is not None else 'unavailable (contract enforces)'
    err_console.print(
        Panel(
            f'[cyan]Repository:[/cyan] {full_name}\n'
            f'[cyan]GitHub ID:[/cyan] {github_id}\n'
            f'[cyan]Fee Hotkey:[/cyan] {fee_hotkey}\n'
            f'[cyan]Current Price:[/cyan] {price_text}',
            title='Repository Registration',
            border_style='blue',
        )
    )

    if not confirm_or_abort(f'Register {full_name} for {price_text}?', yes, default=True):
        return

    try:
        with loading_context('Submitting registration...', as_json):
            tx_hash, error = client.register(github_id, full_name, fee_hotkey, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Registration', tx_hash, error, as_json, github_id=github_id, full_name=full_name)


def _parse_param_pairs(pairs: Tuple[str, ...]) -> Dict[str, int]:
    """Parse name=value pairs into {name: raw contract value}."""
    parsed: Dict[str, int] = {}
    for pair in pairs:
        name, sep, raw = pair.partition('=')
        name = name.strip().lower()
        if not sep or not raw.strip():
            raise click.BadParameter(f'Expected name=value (got {pair!r})', param_hint='PARAMS')
        if name not in PARAM_KEYS:
            raise click.BadParameter(
                f'Unknown param {name!r}. Valid params (key  name):\n{param_table_hint()}',
                param_hint='PARAMS',
            )
        if name in parsed:
            raise click.BadParameter(f'Duplicate param {name!r}', param_hint='PARAMS')
        parsed[name] = parse_param_value(name, raw)
    if not parsed:
        raise click.BadParameter('No params given', param_hint='PARAMS')
    return parsed


@click.command('set-params', cls=StyledCommand)
@click.argument('ref', type=str)
@click.argument('params', type=str, nargs=-1, required=True)
@_tx_options
def repo_set_params(
    ref: str,
    params: Tuple[str, ...],
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Set repository hyperparams as name=value pairs (repo owner only).

    [dim]REF is a GitHub numeric id or owner/name. Values are human units —
    fixed-point params (multipliers, shares) accept decimals and are scaled
    on-chain. One transaction per param; changes are rate-limited per key.[/dim]

    [dim]Examples:
        $ gitt repo set-params entrius/gittensor maintainer_cut=0.1 pr_lookback_days=30
        $ gitt r set-params 987654321 issue_discovery_share=0.25 -y
    [/dim]
    """
    changes = _parse_param_pairs(params)

    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository and bounds...', as_json):
            repo = resolve_repo_ref(ref, client)
            for name, value in changes.items():
                key = PARAM_KEYS[name][0]
                bounds = client.get_bounds(key)
                if bounds and not bounds.min <= value <= bounds.max:
                    raise click.ClickException(
                        f'{name}={format_param_value(key, value)} out of bounds '
                        f'[{format_param_value(key, bounds.min)}, {format_param_value(key, bounds.max)}]'
                    )
    except click.ClickException as e:
        handle_exception(as_json=as_json, message=str(e), error_type='bad_parameter')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    lines = '\n'.join(
        f'[cyan]{name}[/cyan] (key {PARAM_KEYS[name][0]}) = {format_param_value(PARAM_KEYS[name][0], value)}'
        for name, value in changes.items()
    )
    err_console.print(Panel(lines, title=f'Set params on {repo.full_name}', border_style='blue'))

    if not confirm_or_abort(f'Submit {len(changes)} param change(s)?', yes, default=True):
        return

    results = []
    failed = False
    for name, value in changes.items():
        key = PARAM_KEYS[name][0]
        try:
            with loading_context(f'Setting {name}...', as_json):
                tx_hash, error = client.set_param(repo.github_id, key, value, wallet)
        except Exception as e:
            tx_hash, error = None, str(e)
        results.append({'param': name, 'key': key, 'value': value, 'tx_hash': tx_hash, 'error': error})
        if error is not None or tx_hash is None:
            failed = True
            break

    if as_json:
        emit_json({'success': not failed, 'github_id': repo.github_id, 'results': results})
    else:
        for result in results:
            if result['error'] is None and result['tx_hash'] is not None:
                err_console.print(f'[green]✓ {result["param"]}[/green] ({result["tx_hash"]})')
            else:
                err_console.print(f'[red]✗ {result["param"]}: {result["error"] or "failed"}[/red]')
        if failed:
            err_console.print('[yellow]Stopped at first failure; remaining params not submitted.[/yellow]')
    if failed:
        raise SystemExit(1)


def _label_value_callback(ctx: click.Context, param: click.Parameter, value: str) -> int:
    try:
        d = Decimal(value.strip())
    except InvalidOperation:
        raise click.BadParameter(f'Invalid number: {value}')
    if not LABEL_MULT_MIN <= d <= LABEL_MULT_MAX:
        raise click.BadParameter(f'Multiplier must be in [{LABEL_MULT_MIN}, {LABEL_MULT_MAX}] (got {value})')
    scaled = d * FP6
    if scaled != scaled.to_integral_value():
        raise click.BadParameter(f'At most 6 decimal places allowed (got {value})')
    return int(scaled)


@click.command('set-label', cls=StyledCommand)
@click.argument('ref', type=str)
@click.argument('label', type=str)
@click.argument('value', type=str, callback=_label_value_callback)
@_tx_options
def repo_set_label(
    ref: str,
    label: str,
    value: int,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Set or update a label multiplier in [0.5, 2] (repo owner only).

    [dim]Examples:
        $ gitt repo set-label entrius/gittensor bug 1.5
        $ gitt r set-label 987654321 "good first issue" 0.8 -y
    [/dim]
    """
    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository...', as_json):
            repo = resolve_repo_ref(ref, client)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if not confirm_or_abort(f'Set label {label!r} = {format_param_value(2, value)} on {repo.full_name}?', yes, True):
        return

    try:
        with loading_context('Submitting...', as_json):
            tx_hash, error = client.set_label_multiplier(repo.github_id, label, value, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Label multiplier update', tx_hash, error, as_json, github_id=repo.github_id, label=label)


@click.command('remove-label', cls=StyledCommand)
@click.argument('ref', type=str)
@click.argument('label', type=str)
@_tx_options
def repo_remove_label(
    ref: str,
    label: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Remove a label multiplier (repo owner only).

    [dim]Examples:
        $ gitt repo remove-label entrius/gittensor bug
    [/dim]
    """
    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository...', as_json):
            repo = resolve_repo_ref(ref, client)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if not confirm_or_abort(f'Remove label {label!r} from {repo.full_name}?', yes, True):
        return

    try:
        with loading_context('Submitting...', as_json):
            tx_hash, error = client.remove_label_multiplier(repo.github_id, label, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Label multiplier removal', tx_hash, error, as_json, github_id=repo.github_id, label=label)


@click.command('set-branch-patterns', cls=StyledCommand)
@click.argument('ref', type=str)
@click.argument('patterns', type=str, nargs=-1)
@_tx_options
def repo_set_branch_patterns(
    ref: str,
    patterns: Tuple[str, ...],
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Replace the additional acceptable branch patterns (repo owner only).

    [dim]Up to 4 patterns matching ^[a-z0-9][a-z0-9._/-]*\\*?$ — '*' only as a
    suffix, bare '*' rejected. No patterns clears the list.[/dim]

    [dim]Examples:
        $ gitt repo set-branch-patterns entrius/gittensor develop "release/*"
        $ gitt r set-branch-patterns 987654321 -y   # clear
    [/dim]
    """
    cleaned: List[str] = []
    for pattern in patterns:
        pattern = pattern.strip()
        if not BRANCH_PATTERN_RE.match(pattern):
            raise click.BadParameter(f'Invalid pattern {pattern!r}', param_hint='PATTERNS')
        if pattern in cleaned:
            raise click.BadParameter(f'Duplicate pattern {pattern!r}', param_hint='PATTERNS')
        cleaned.append(pattern)
    if len(cleaned) > MAX_PATTERNS:
        raise click.BadParameter(f'At most {MAX_PATTERNS} patterns allowed', param_hint='PATTERNS')

    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository...', as_json):
            repo = resolve_repo_ref(ref, client)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    action = f'Set branch patterns to [{", ".join(cleaned)}]' if cleaned else 'Clear all branch patterns'
    if not confirm_or_abort(f'{action} on {repo.full_name}?', yes, True):
        return

    try:
        with loading_context('Submitting...', as_json):
            tx_hash, error = client.set_branch_patterns(repo.github_id, cleaned, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Branch pattern update', tx_hash, error, as_json, github_id=repo.github_id, patterns=cleaned)


@click.command('update-name', cls=StyledCommand)
@click.argument('ref', type=str)
@click.argument('new_name', type=REPO)
@_tx_options
def repo_update_name(
    ref: str,
    new_name: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Update a repo's owner/name after a GitHub rename (repo owner only, rate-limited).

    [dim]The GitHub numeric id stays the primary key; this follows a rename.[/dim]

    [dim]Examples:
        $ gitt repo update-name 987654321 neworg/newname
    [/dim]
    """
    new_name = new_name.lower()
    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository...', as_json):
            repo = resolve_repo_ref(ref, client)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if not confirm_or_abort(f'Rename {repo.full_name} -> {new_name}?', yes, True):
        return

    try:
        with loading_context('Submitting...', as_json):
            tx_hash, error = client.update_full_name(repo.github_id, new_name, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Rename', tx_hash, error, as_json, github_id=repo.github_id, full_name=new_name)


@click.command('transfer', cls=StyledCommand)
@click.argument('ref', type=str)
@click.argument('new_owner', type=SS58)
@_tx_options
def repo_transfer(
    ref: str,
    new_owner: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Transfer repository ownership to another coldkey (repo owner only).

    [dim]Examples:
        $ gitt repo transfer entrius/gittensor 5Hxxx...
    [/dim]
    """
    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository...', as_json):
            repo = resolve_repo_ref(ref, client)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    err_console.print(
        Panel(
            f'[cyan]Repository:[/cyan] {repo.full_name}\n'
            f'[cyan]Current Owner:[/cyan] {repo.owner}\n'
            f'[cyan]New Owner:[/cyan] {new_owner}',
            title='Transfer Ownership',
            border_style='yellow',
        )
    )
    if not confirm_or_abort(f'Transfer ownership of {repo.full_name} to {new_owner}?', yes):
        return

    try:
        with loading_context('Submitting...', as_json):
            tx_hash, error = client.transfer_ownership(repo.github_id, new_owner, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Ownership transfer', tx_hash, error, as_json, github_id=repo.github_id, new_owner=new_owner)


@click.command('deregister', cls=StyledCommand)
@click.argument('ref', type=str)
@_tx_options
def repo_deregister(
    ref: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
    yes: bool,
):
    """Deregister a repository, freeing its slot — NO refund (repo owner only).

    [dim]Re-registration pays a fresh fee and restarts immunity.[/dim]

    [dim]Examples:
        $ gitt repo deregister entrius/gittensor
    [/dim]
    """
    try:
        wallet, _subtensor, client = _connect(contract, network, rpc_url, wallet_name, wallet_hotkey, as_json)
        with loading_context('Resolving repository...', as_json):
            repo = resolve_repo_ref(ref, client)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    err_console.print(f'[yellow]Deregistering {repo.full_name} frees its slot with no refund.[/yellow]')
    if not confirm_or_abort(f'Deregister {repo.full_name} (id {repo.github_id})?', yes):
        return

    try:
        with loading_context('Submitting...', as_json):
            tx_hash, error = client.deregister(repo.github_id, wallet)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
    emit_tx_result('Deregistration', tx_hash, error, as_json, github_id=repo.github_id)
