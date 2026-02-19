# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Top-level mutation commands for issue CLI

Commands:
    gitt register
    gitt harvest
"""

from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
from rich.panel import Panel

from .helpers import (
    ALPHA_RAW_UNIT,
    console,
    format_alpha,
    get_contract_address,
    load_config,
    print_error,
    print_success,
    resolve_network,
    validate_repo_format,
    verify_github_issue,
    verify_github_repo,
)


@click.command('register')
@click.option(
    '--repo',
    required=True,
    help='Repository in owner/repo format (e.g., opentensor/btcli)',
)
@click.option(
    '--issue',
    'issue_number',
    required=True,
    type=int,
    help='GitHub issue number (1-999,999)',
)
@click.option(
    '--bounty',
    required=True,
    type=str,
    help='Bounty amount in ALPHA tokens (min 10)',
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
    help='Wallet name (must be contract owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--github-pat',
    default='',
    help='GitHub PAT for verification (optional)',
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
    github_pat: str,
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
        gitt register --repo opentensor/btcli --issue 144 --bounty 100
        gitt register --repo tensorflow/tensorflow --issue 12345 --bounty 50
    """
    console.print('\n[bold cyan]Register Issue for Bounty[/bold cyan]\n')

    # 1. Validate repository format
    if not validate_repo_format(repo):
        print_error('Repository must be in owner/repo format (alphanumeric, no spaces).')
        return

    # 2. Validate issue number
    if issue_number < 1 or issue_number >= 1_000_000:
        print_error('Issue number must be between 1 and 999,999.')
        return

    # 3. Validate bounty amount (min and precision using Decimal)
    try:
        bounty_dec = Decimal(bounty)
    except InvalidOperation:
        print_error(f'Invalid bounty amount: {bounty}')
        return

    if bounty_dec < Decimal('10.0'):
        print_error('Minimum bounty is 10.0 ALPHA.')
        return

    # Check for excessive precision (> 9 decimals)
    if bounty_dec.as_tuple().exponent < -9:
        print_error('Bounty amount exceeds maximum precision (9 decimal places).')
        return

    # 4. Verify on GitHub
    pat = github_pat or load_config().get('github_pat')
    with console.status('[yellow]Verifying issue on GitHub...[/yellow]'):
        issue_info = verify_github_issue(repo, issue_number, pat)

    if not issue_info:
        # If issue not found, check if repo exists at least
        with console.status('[yellow]Issue not found. Checking if repository exists...[/yellow]'):
            repo_exists = verify_github_repo(repo, pat)

        if not repo_exists:
            print_error(f'Repository "{repo}" does not exist on GitHub.')
        else:
            print_error(f'Issue #{issue_number} does not exist in repository "{repo}".')
        return

    if issue_info.get('is_pull_request'):
        print_error('The specified issue is a pull request. Gittensor only supports issues.')
        return

    if issue_info.get('state') != 'open':
        print_error(f'Issue #{issue_number} is already {issue_info.get("state")}.')
        return

    # Construct GitHub URL
    github_url = f'https://github.com/{repo}/issues/{issue_number}'

    # Display registration details
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    config = load_config()

    console.print(
        Panel(
            f'[cyan]Repository:[/cyan] {repo}\n'
            f'[cyan]Issue Name:[/cyan] {issue_info.get("title")}\n'
            f'[cyan]Issue Number:[/cyan] #{issue_number}\n'
            f'[cyan]GitHub URL:[/cyan] {github_url}\n'
            f'[cyan]Target Bounty:[/cyan] {bounty_dec:.4f} ALPHA\n'
            f'[cyan]Network:[/cyan] {network_name}\n'
            f'[cyan]RPC Endpoint:[/cyan] {ws_endpoint}\n'
            f'[cyan]Contract:[/cyan] {contract_addr if contract_addr else "(not configured)"}',
            title='Issue Registration',
            border_style='blue',
        )
    )

    if not contract_addr:
        print_error('Contract address not configured. Set via: gitt config set contract_address <address>')
        return

    if not click.confirm('\nProceed with registration?', default=True):
        console.print('[yellow]Registration cancelled.[/yellow]')
        return

    # Perform actual contract call (on-chain transaction)
    with console.status('[yellow]Submitting on-chain transaction...[/yellow]'):
        try:
            import bittensor as bt
            from substrateinterface import Keypair, SubstrateInterface
            from substrateinterface.contracts import ContractInstance

            # Connect to subtensor
            substrate = SubstrateInterface(url=ws_endpoint)

            # CLI flags override config; fall back to config if not explicitly supplied
            effective_wallet = wallet_name if wallet_name != 'default' else config.get('wallet', wallet_name)
            effective_hotkey = wallet_hotkey if wallet_hotkey != 'default' else config.get('hotkey', wallet_hotkey)

            # For local development, check config first, then fall back to //Alice
            if network_name.lower() == 'local' and effective_wallet == 'default' and effective_hotkey == 'default':
                keypair = Keypair.create_from_uri('//Alice')
            else:
                # Load wallet from config or CLI args
                wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
                keypair = wallet.coldkey

            # Load contract
            contract_metadata = (
                Path(__file__).parent.parent.parent.parent
                / 'smart-contracts'
                / 'issues-v0'
                / 'target'
                / 'ink'
                / 'issue_bounty_manager.contract'
            )
            if not contract_metadata.exists():
                print_error(f'Contract metadata not found at {contract_metadata}')
                return

            contract_instance = ContractInstance.create_from_address(
                contract_address=contract_addr,
                metadata_file=str(contract_metadata),
                substrate=substrate,
            )

            # Convert bounty to contract units (9 decimals for ALPHA)
            bounty_amount = int(bounty_dec * ALPHA_RAW_UNIT)

            result = contract_instance.exec(
                keypair,
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
                print_error('Transaction failed: Contract rejected the request')

                error_info = getattr(result, 'error_message', None)
                is_revert = error_info and isinstance(error_info, dict) and error_info.get('name') == 'ContractReverted'

                if is_revert:
                    console.print('[yellow]Possible reasons:[/yellow]')
                    console.print('  • Issue already registered (same repo + issue number)')
                    console.print('  • Bounty too low (minimum 10 ALPHA)')
                    console.print('  • Invalid repository format (must be owner/repo)')
                    console.print('  • Caller is not the contract owner')
                elif error_info:
                    console.print(f'[red]Error: {error_info}[/red]')

                console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
                return

            print_success('Issue registered successfully!')
            console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
            console.print('[dim]Issue will be visible once bounty is funded via harvest_emissions()[/dim]')

        except ImportError as e:
            print_error(f'Missing dependency - {e}')
        except Exception as e:
            error_msg = str(e)
            if 'ContractReverted' in error_msg:
                print_error('Transaction failed: Contract rejected the request')
                console.print('[yellow]Possible reasons:[/yellow]')
                console.print('  • Issue already registered (same repo + issue number)')
                console.print('  • Bounty too low (minimum 10 ALPHA)')
                console.print('  • Caller is not the contract owner')
            else:
                print_error(f'Error registering issue: {e}')


@click.command('harvest')
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='validator',
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
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output')
def issue_harvest(wallet_name: str, wallet_hotkey: str, network: str, rpc_url: str, contract: str, verbose: bool):
    """
    Manually trigger emission harvest from contract treasury.

    This command is permissionless - any wallet can trigger it.
    The contract handles emission collection and distribution internally.

    \b
    Examples:
        gitt harvest
        gitt harvest --verbose
        gitt harvest --wallet-name mywallet --wallet-hotkey mykey
    """
    console.print('\n[bold cyan]Manual Emission Harvest[/bold cyan]\n')

    # Get configuration
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        console.print('[dim]Set CONTRACT_ADDRESS env var or run ./up.sh --issues[/dim]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey}[/dim]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        # Load wallet
        console.print('[yellow]Loading wallet...[/yellow]')
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        hotkey_addr = wallet.hotkey.ss58_address
        console.print(f'[green]Hotkey address:[/green] {hotkey_addr}')

        # Connect to subtensor
        console.print('\n[yellow]Connecting to subtensor...[/yellow]')
        subtensor = bt.Subtensor(network=ws_endpoint)

        # Show wallet balance (informational only)
        if verbose:
            try:
                balance = subtensor.get_balance(hotkey_addr)
                console.print(f'[dim]Wallet balance: {balance}[/dim]')
            except Exception as e:
                console.print(f'[dim]Could not fetch balance: {e}[/dim]')

        # Create contract client
        console.print('\n[yellow]Initializing contract client...[/yellow]')
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        if verbose:
            # Show contract state
            console.print('[dim]Reading contract state...[/dim]')
            try:
                alpha_pool = client.get_alpha_pool()
                pending = client.get_treasury_stake()
                last_harvest = client.get_last_harvest_block()
                current_block = subtensor.get_current_block()

                console.print(f'[dim]Alpha pool: {alpha_pool / 1e9:.4f} ALPHA[/dim]')
                console.print(f'[dim]Treasury stake: {pending / 1e9:.4f} ALPHA[/dim]')
                console.print(f'[dim]Last harvest block: {last_harvest}[/dim]')
                console.print(f'[dim]Current block: {current_block}[/dim]')
                if last_harvest > 0:
                    console.print(f'[dim]Blocks since harvest: {current_block - last_harvest}[/dim]')
            except Exception as e:
                console.print(f'[yellow]Warning: Could not read contract state: {e}[/yellow]')

        # Attempt harvest
        console.print('\n[yellow]Calling harvest_emissions()...[/yellow]')
        result = client.harvest_emissions(wallet)

        if result:
            if result.get('status') == 'success':
                console.print('\n[green]Harvest succeeded![/green]')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                console.print('[dim]Treasury stake processed. Excess emissions recycled if any.[/dim]')
            elif result.get('status') == 'partial':
                console.print('\n[yellow]Harvest completed but recycling failed![/yellow]')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                console.print(f'[red]Error: {result.get("error", "Unknown")}[/red]')
                console.print('[dim]Check proxy permissions: contract needs NonCritical proxy.[/dim]')
            elif result.get('status') == 'failed':
                console.print('\n[red]Harvest failed![/red]')
                console.print(f'[red]Error: {result.get("error", "Unknown error")}[/red]')
            else:
                console.print(f'\n[yellow]Harvest result: {result}[/yellow]')
        else:
            console.print('\n[red]Harvest returned None - check logs for details.[/red]')
            console.print('[dim]Run with --verbose for more information.[/dim]')

    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
        console.print('[dim]Install with: pip install bittensor substrate-interface[/dim]')
    except Exception as e:
        import traceback

        console.print(f'\n[red]Error during harvest: {type(e).__name__}: {e}[/red]')
        if verbose:
            console.print(f'[dim]Full traceback:\n{traceback.format_exc()}[/dim]')
        else:
            console.print('[dim]Run with --verbose for full traceback.[/dim]')
