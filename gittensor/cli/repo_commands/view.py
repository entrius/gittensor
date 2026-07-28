# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Read-only repo registry commands.

Commands:
    gitt repo list
    gitt repo show <id-or-name>
    gitt repo price
"""

import click
from rich.panel import Panel
from rich.table import Table

from gittensor.cli.core.help import StyledCommand
from gittensor.cli.core.helpers import (
    console,
    err_console,
    format_alpha,
    handle_exception,
    loading_context,
    print_network_header,
    with_cli_behavior_options,
    with_network_contract_options,
)
from gittensor.cli.core.json_output import emit_json

from .helpers import (
    PARAM_NAMES_BY_KEY,
    format_param_value,
    make_registry_client,
    resolve_repo_ref,
    resolve_repos_contract_and_network,
)

CONTRACT_HELP = 'Repos contract address (uses config if empty)'


def _repo_dict(repo, block: int, immunity_period: int) -> dict:
    immune_until = repo.reg_block + immunity_period
    return {
        'github_id': repo.github_id,
        'full_name': repo.full_name,
        'owner': repo.owner,
        'reg_block': repo.reg_block,
        'active': repo.active,
        'immune': block < immune_until,
        'immune_until': immune_until,
    }


@click.command('list', cls=StyledCommand)
@with_network_contract_options(CONTRACT_HELP)
@with_cli_behavior_options(include_json=True)
def repo_list(network: str, rpc_url: str, contract: str, as_json: bool):
    """List registered repositories with immunity and active status.

    [dim]Examples:
        $ gitt repo list
        $ gitt r list --network test --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)

    try:
        with loading_context('Reading repository registry...', as_json):
            subtensor, client = make_registry_client(contract_addr, ws_endpoint)
            packed = client.get_registry()
            if packed is None:
                raise click.ClickException('Could not read registry storage from the contract.')
            repos = client.get_all_repos()
            block = subtensor.get_current_block()
    except click.ClickException as e:
        handle_exception(as_json=as_json, message=str(e), error_type='read_failed')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    immunity = packed.constants.immunity_period
    rows = [_repo_dict(repo, block, immunity) for repo in sorted(repos, key=lambda r: r.reg_block)]

    if as_json:
        emit_json(
            {
                'success': True,
                'block': block,
                'count': len(rows),
                'max_repos': packed.constants.max_repos,
                'repos': rows,
            }
        )
        return

    if not rows:
        err_console.print('[yellow]No repositories registered.[/yellow]')
        return

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='dim', justify='right')
    table.add_column('Repository', style='cyan')
    table.add_column('Owner')
    table.add_column('Reg Block', justify='right')
    table.add_column('Immune', justify='center')
    table.add_column('Active', justify='center')
    for row in rows:
        owner = row['owner']
        table.add_row(
            str(row['github_id']),
            row['full_name'],
            f'{owner[:8]}...{owner[-6:]}',
            str(row['reg_block']),
            f'[green]until {row["immune_until"]}[/green]' if row['immune'] else '[dim]no[/dim]',
            '[green]yes[/green]' if row['active'] else '[red]no[/red]',
        )
    console.print(table)
    console.print(f'\n[green]Registered:[/green] {len(rows)} / {packed.constants.max_repos} slots')


@click.command('show', cls=StyledCommand)
@click.argument('ref', type=str)
@with_network_contract_options(CONTRACT_HELP)
@with_cli_behavior_options(include_json=True)
def repo_show(ref: str, network: str, rpc_url: str, contract: str, as_json: bool):
    """Show one repository: record, hyperparams, label multipliers, branch patterns.

    [dim]REF is a GitHub numeric id or owner/name.[/dim]

    [dim]Examples:
        $ gitt repo show entrius/gittensor
        $ gitt r show 987654321 --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)

    try:
        with loading_context('Reading repository...', as_json):
            subtensor, client = make_registry_client(contract_addr, ws_endpoint)
            packed = client.get_registry()
            if packed is None:
                raise click.ClickException('Could not read registry storage from the contract.')
            repo = resolve_repo_ref(ref, client)
            params = client.get_params(repo.github_id)
            labels = client.get_label_multipliers(repo.github_id)
            patterns = client.get_branch_patterns(repo.github_id)
            block = subtensor.get_current_block()
    except click.ClickException as e:
        handle_exception(as_json=as_json, message=str(e), error_type='read_failed')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    info = _repo_dict(repo, block, packed.constants.immunity_period)

    if as_json:
        emit_json(
            {
                'success': True,
                'repo': info,
                'params': {PARAM_NAMES_BY_KEY.get(key, str(key)): value for key, value in sorted(params.items())},
                'label_multipliers': labels,
                'branch_patterns': patterns,
            }
        )
        return

    immunity = f'until block {info["immune_until"]}' if info['immune'] else 'expired'
    console.print(
        Panel(
            f'[cyan]GitHub ID:[/cyan] {repo.github_id}\n'
            f'[cyan]Repository:[/cyan] {repo.full_name}\n'
            f'[cyan]Owner:[/cyan] {repo.owner}\n'
            f'[cyan]Registered:[/cyan] block {repo.reg_block}\n'
            f'[cyan]Immunity:[/cyan] {immunity}\n'
            f'[cyan]Active:[/cyan] {"yes" if repo.active else "no"}',
            title=repo.full_name,
            border_style='blue',
        )
    )

    if params:
        table = Table(title='Hyperparams', show_header=True, header_style='bold magenta')
        table.add_column('Key', style='dim', justify='right')
        table.add_column('Param', style='cyan')
        table.add_column('Value', justify='right')
        for key, value in sorted(params.items()):
            table.add_row(str(key), PARAM_NAMES_BY_KEY.get(key, '?'), format_param_value(key, value))
        console.print(table)
    else:
        err_console.print('[dim]No hyperparam overrides (defaults apply).[/dim]')

    if labels:
        table = Table(title='Label multipliers', show_header=True, header_style='bold magenta')
        table.add_column('Label', style='cyan')
        table.add_column('Multiplier', justify='right')
        for label, value in sorted(labels.items()):
            table.add_row(label, format_param_value(2, value))
        console.print(table)

    if patterns:
        console.print(f'[cyan]Branch patterns:[/cyan] {", ".join(patterns)}')


@click.command('price', cls=StyledCommand)
@with_network_contract_options(CONTRACT_HELP)
@with_cli_behavior_options(include_json=True)
def repo_price(network: str, rpc_url: str, contract: str, as_json: bool):
    """Show the current registration price quote.

    [dim]Lazy-decay quote computed off pinned contract state — matches what
    `gitt repo register` would pay right now.[/dim]

    [dim]Examples:
        $ gitt repo price
        $ gitt r price --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = resolve_repos_contract_and_network(contract, network, rpc_url)
    print_network_header(network_name, contract_addr)

    try:
        with loading_context('Quoting registration price...', as_json):
            _subtensor, client = make_registry_client(contract_addr, ws_endpoint)
            quote = client.quote_price()
            if quote is None:
                raise click.ClickException('Could not compute a price quote from contract state.')
    except click.ClickException as e:
        handle_exception(as_json=as_json, message=str(e), error_type='read_failed')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))

    if as_json:
        emit_json({'success': True, 'price_raw': quote, 'price_alpha': format_alpha(quote, 4)})
        return
    console.print(f'[green]Registration price:[/green] {format_alpha(quote, 4)} ALPHA')
