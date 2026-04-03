# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Top-level mutation commands for issue CLI

Commands:
    gitt register
"""

from pathlib import Path

import click
from rich.panel import Panel

from .helpers import (
    MAX_ISSUE_NUMBER,
    _is_interactive,
    console,
    format_alpha,
    get_contract_address,
    load_config,
    print_error,
    print_success,
    resolve_network,
    validate_bounty_amount,
    validate_github_issue,
    validate_repository,
)


@click.command('register')
@click.option(
    '--repo',
    required=True,
    help='Repository in owner/repo format (e.g., latent-to/btcli)',
)
@click.option(
    '--issue',
    'issue_number',
    required=True,
    type=int,
    help='GitHub issue number',
)
@click.option(
    '--bounty',
    required=True,
    type=str,
    help='Bounty amount in ALPHA (e.g. 10 or 10.5)',
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
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--yes',
    '-y',
    is_flag=True,
    help='Skip confirmation prompt (non-interactive/CI)',
)
def issue_register(
    repo: str,
    issue_number: int,
    bounty: str,
    network: str,
    rpc_url: str,
    contract: str,
    wallet_name: str,
    wallet_hotkey: str,
    yes: bool,
):
    """
    Register a new issue with a bounty (OWNER ONLY).

    This command registers a GitHub issue on the smart contract
    with a target bounty amount. Only the contract owner can
    register new issues.

    \b
    Arguments:
        --repo: Repository in owner/repo format
        --issue: GitHub issue number
        --bounty: Target bounty amount in ALPHA

    \b
    Examples:
        gitt issues register --repo latent-to/btcli --issue 144 --bounty 100
        gitt i reg --repo tensorflow/tensorflow --issue 12345 --bounty 50
        gitt i reg --repo owner/repo --issue 1 --bounty 10 -y
    """
    console.print('\n[bold cyan]Register Issue for Bounty[/bold cyan]\n')

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    config = load_config()

    if not contract_addr:
        raise click.ClickException(
            'Contract address not configured. Run ./up.sh --issues to deploy the contract first.'
        )

    # Validate inputs before showing summary
    try:
        owner, repo_name = validate_repository(repo)
        bounty_amount = validate_bounty_amount(bounty)
        if issue_number < 1 or issue_number > MAX_ISSUE_NUMBER:
            raise click.BadParameter(
                f'Issue number must be between 1 and {MAX_ISSUE_NUMBER} (got {issue_number})',
                param_hint='--issue',
            )
        validate_github_issue(owner, repo_name, issue_number)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    github_url = f'https://github.com/{repo}/issues/{issue_number}'

    console.print(
        Panel(
            f'[cyan]Repository:[/cyan] {repo}\n'
            f'[cyan]Issue Number:[/cyan] #{issue_number}\n'
            f'[cyan]GitHub URL:[/cyan] {github_url}\n'
            f'[cyan]Target Bounty:[/cyan] {format_alpha(bounty_amount, 2)} ALPHA\n'
            f'[cyan]Network:[/cyan] {network_name}\n'
            f'[cyan]RPC Endpoint:[/cyan] {ws_endpoint}\n'
            f'[cyan]Contract:[/cyan] {contract_addr}',
            title='Issue Registration',
            border_style='blue',
        )
    )

    skip_confirm = yes or not _is_interactive()
    if not skip_confirm and not click.confirm('\nProceed with registration?', default=True):
        console.print('[yellow]Registration cancelled.[/yellow]')
        return

    try:
        import bittensor as bt
        from substrateinterface import Keypair, SubstrateInterface
        from substrateinterface.contracts import ContractInstance

        with console.status('[bold cyan]Connecting to network...', spinner='dots'):
            substrate = SubstrateInterface(url=ws_endpoint)

        # CLI flags override config; fall back to config if not explicitly supplied
        effective_wallet = wallet_name if wallet_name != 'default' else config.get('wallet', wallet_name)
        effective_hotkey = wallet_hotkey if wallet_hotkey != 'default' else config.get('hotkey', wallet_hotkey)

        # For local development, check config first, then fall back to //Alice
        if network_name.lower() == 'local' and effective_wallet == 'default' and effective_hotkey == 'default':
            console.print('[dim]Using //Alice for local development (no config set)...[/dim]')
            keypair = Keypair.create_from_uri('//Alice')
        else:
            # Load wallet from config or CLI args
            console.print(f'[dim]Loading wallet {effective_wallet}/{effective_hotkey}...[/dim]')
            wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
            # Use COLDKEY for owner-only operations (register_issue requires owner)
            # Contract owner is set to deployer's coldkey during contract instantiation
            keypair = wallet.coldkey

        # Load contract
        # Go up 4 levels: mutations.py -> issue_commands -> cli -> gittensor -> REPO_ROOT
        contract_metadata = (
            Path(__file__).parent.parent.parent.parent
            / 'smart-contracts'
            / 'issues-v0'
            / 'target'
            / 'ink'
            / 'issue_bounty_manager.contract'
        )
        if not contract_metadata.exists():
            console.print(f'[red]Error: Contract metadata not found at {contract_metadata}[/red]')
            return

        contract_instance = ContractInstance.create_from_address(
            contract_address=contract_addr,
            metadata_file=str(contract_metadata),
            substrate=substrate,
        )

        console.print('[dim]Submitting transaction...[/dim]')

        result = contract_instance.exec(
            keypair,  # type: ignore[arg-type]
            'register_issue',
            args={
                'github_url': github_url,
                'repository_full_name': repo,
                'issue_number': issue_number,
                'target_bounty': bounty_amount,
            },
            gas_limit={'ref_time': 10_000_000_000, 'proof_size': 1_000_000},
        )

        # Check if transaction was successful
        if hasattr(result, 'is_success') and not result.is_success:
            error_info = getattr(result, 'error_message', None)
            is_revert = error_info and isinstance(error_info, dict) and error_info.get('name') == 'ContractReverted'

            if is_revert:
                print_error('Contract rejected the request')
                console.print('[yellow]Possible reasons:[/yellow]')
                console.print('  \u2022 Issue already registered (same repo + issue number)')
                console.print('  \u2022 Bounty too low (minimum 10 ALPHA)')
                console.print('  \u2022 Caller is not the contract owner')
            elif error_info:
                print_error(str(error_info))

            console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
            return

        print_success('Issue registered successfully!')
        console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
        console.print('[dim]Issue registered on contract.[/dim]')

    except ImportError as e:
        print_error(f'Missing dependency - {e}')
        console.print('[dim]Install with: pip install substrate-interface bittensor[/dim]')
    except Exception as e:
        error_msg = str(e)
        if 'ContractReverted' in error_msg:
            print_error('Contract rejected the request')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  \u2022 Issue already registered (same repo + issue number)')
            console.print('  \u2022 Bounty too low (minimum 10 ALPHA)')
            console.print('  \u2022 Caller is not the contract owner')
        else:
            print_error(f'Error registering issue: {e}')
