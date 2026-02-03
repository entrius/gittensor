# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Top-level mutation commands for issue CLI.

Commands:
    gitt issue register
    gitt issue harvest
    gitt issue deposit
    gitt issue prefer
    gitt issue enroll
    gitt issue withdraw
"""

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from .helpers import (
    console,
    load_config,
    load_preferences,
    save_preferences,
    clear_preferences,
    get_contract_address,
    get_ws_endpoint,
    ISSUE_PREFERENCES_FILE,
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
    help='GitHub issue number',
)
@click.option(
    '--bounty',
    required=True,
    type=float,
    help='Bounty amount in ALPHA tokens',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses default if empty)',
)
@click.option('--testnet', is_flag=True, help='Use testnet contract address')
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be contract owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def issue_register(
    repo: str,
    issue_number: int,
    bounty: float,
    rpc_url: str,
    contract: str,
    testnet: bool,
    wallet_name: str,
    wallet_hotkey: str,
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
        gitt issue register --repo opentensor/btcli --issue 144 --bounty 100
        gitt issue register --repo tensorflow/tensorflow --issue 12345 --bounty 50 --testnet
    """
    console.print('\n[bold cyan]Register Issue for Competition[/bold cyan]\n')

    # Validate repo format
    if '/' not in repo:
        console.print('[red]Error: Repository must be in owner/repo format[/red]')
        return

    # Construct GitHub URL
    github_url = f'https://github.com/{repo}/issues/{issue_number}'

    # Display registration details
    # Get contract address and endpoint from env/config if not provided
    contract_addr = get_contract_address(contract, testnet)
    ws_endpoint = get_ws_endpoint(rpc_url)

    # Determine network name from config
    config = load_config()
    network_name = config.get('network', 'mainnet').capitalize()
    if testnet:
        network_name = 'Testnet'

    console.print(Panel(
        f'[cyan]Repository:[/cyan] {repo}\n'
        f'[cyan]Issue Number:[/cyan] #{issue_number}\n'
        f'[cyan]GitHub URL:[/cyan] {github_url}\n'
        f'[cyan]Target Bounty:[/cyan] {bounty:.2f} ALPHA\n'
        f'[cyan]Network:[/cyan] {network_name}\n'
        f'[cyan]WS Endpoint:[/cyan] {ws_endpoint}\n'
        f'[cyan]Contract:[/cyan] {contract_addr if contract_addr else "(not configured)"}',
        title='Issue Registration',
        border_style='blue',
    ))

    if not contract_addr:
        console.print('\n[red]Error: Contract address not configured.[/red]')
        console.print('[dim]Run ./up.sh --issues to deploy the contract first.[/dim]')
        return

    if not click.confirm('\nProceed with registration?', default=True):
        console.print('[yellow]Registration cancelled.[/yellow]')
        return

    # Perform actual contract call (on-chain transaction)
    console.print('\n[yellow]Submitting on-chain transaction to contract...[/yellow]')

    try:
        from substrateinterface import SubstrateInterface, Keypair
        from substrateinterface.contracts import ContractInstance
        import bittensor as bt

        # Connect to subtensor
        console.print(f'[dim]Connecting to {ws_endpoint}...[/dim]')
        substrate = SubstrateInterface(url=ws_endpoint)

        # Load wallet from config or CLI args
        effective_wallet = config.get('wallet', wallet_name)
        effective_hotkey = config.get('hotkey', wallet_hotkey)

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
        contract_metadata = Path(__file__).parent.parent.parent / 'smart-contracts' / 'ink' / 'target' / 'ink' / 'issue_bounty_manager.contract'
        if not contract_metadata.exists():
            console.print(f'[red]Error: Contract metadata not found at {contract_metadata}[/red]')
            return

        contract = ContractInstance.create_from_address(
            contract_address=contract_addr,
            metadata_file=str(contract_metadata),
            substrate=substrate,
        )

        # Convert bounty to contract units (9 decimals for ALPHA)
        bounty_amount = int(bounty * 1_000_000_000)

        console.print('[yellow]Calling register_issue on contract...[/yellow]')

        result = contract.exec(
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
            console.print(f'\n[red]Transaction failed![/red]')
            if hasattr(result, 'error_message'):
                console.print(f'[red]Error: {result.error_message}[/red]')
            console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
            return

        console.print(f'\n[green]Issue registered successfully![/green]')
        console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
        console.print(f'[dim]Issue will be visible once bounty is funded via depositToPool()[/dim]')

    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
        console.print('[dim]Install with: pip install substrate-interface bittensor[/dim]')
    except Exception as e:
        console.print(f'[red]Error registering issue: {e}[/red]')


@click.command('harvest')
@click.option(
    '--wallet-name',
    default='validator',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output')
def issue_harvest(wallet_name: str, wallet_hotkey: str, rpc_url: str, contract: str, verbose: bool):
    """
    Manually trigger emission harvest from contract treasury.

    This command is permissionless - any wallet can trigger it.
    The contract handles emission collection and distribution internally.

    \b
    Examples:
        gitt issue harvest
        gitt issue harvest --verbose
        gitt issue harvest --wallet-name mywallet --wallet-hotkey mykey
    """
    console.print('\n[bold cyan]Manual Emission Harvest[/bold cyan]\n')

    # Get configuration
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        console.print('[dim]Set CONTRACT_ADDRESS env var or run ./up.sh --issues[/dim]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Endpoint: {ws_endpoint}[/dim]')
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
        console.print(f'\n[yellow]Connecting to subtensor...[/yellow]')
        subtensor = bt.Subtensor(network=ws_endpoint)

        # Show wallet balance (informational only)
        if verbose:
            try:
                balance = subtensor.get_balance(hotkey_addr)
                console.print(f'[dim]Wallet balance: {balance}[/dim]')
            except Exception as e:
                console.print(f'[dim]Could not fetch balance: {e}[/dim]')

        # Create contract client
        console.print(f'\n[yellow]Initializing contract client...[/yellow]')
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
        console.print(f'\n[yellow]Calling harvest_emissions()...[/yellow]')
        result = client.harvest_emissions(wallet)

        if result:
            if result.get('status') == 'success':
                console.print(f'\n[green]Harvest succeeded![/green]')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                if result.get('recycled'):
                    console.print('[dim]Emissions recycled (tokens destroyed via recycle_alpha).[/dim]')
                else:
                    console.print('[dim]No emissions to recycle.[/dim]')
            elif result.get('status') == 'partial':
                console.print(f'\n[yellow]Harvest completed but recycling failed![/yellow]')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                console.print(f'[red]Error: {result.get("error", "Unknown")}[/red]')
                console.print('[dim]Check proxy permissions: contract needs NonCritical proxy.[/dim]')
            elif result.get('status') == 'failed':
                console.print(f'\n[red]Harvest failed![/red]')
                console.print(f'[red]Error: {result.get("error", "Unknown error")}[/red]')
            else:
                console.print(f'\n[yellow]Harvest result: {result}[/yellow]')
        else:
            console.print(f'\n[red]Harvest returned None - check logs for details.[/red]')
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


@click.command('deposit')
@click.argument('issue_id', type=int)
@click.argument('amount', type=float)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
def issue_deposit(
    issue_id: int,
    amount: float,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Deposit funds directly to an issue's bounty (anyone can fund).

    \b
    Arguments:
        ISSUE_ID: Issue to fund
        AMOUNT: Amount in ALPHA tokens

    \b
    Examples:
        gitt issue deposit 1 50.0
        gitt i deposit 42 100
    """
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    # Convert to ALPHA units (9 decimals)
    amount_in_units = int(amount * 1_000_000_000)

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Depositing {amount} ALPHA to issue {issue_id}...[/yellow]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        result = client.deposit_to_issue(issue_id, amount_in_units, wallet)
        if result:
            console.print(f'[green]Deposit successful![/green]')
        else:
            console.print('[red]Deposit failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@click.command('prefer')
@click.argument('issue_ids', nargs=-1, type=int)
@click.option('--clear', is_flag=True, help='Clear existing preferences before adding')
def issue_prefer(issue_ids: tuple, clear: bool):
    """
    Set ranked issue preferences (most preferred first).

    Your preferences determine which issues you'll be matched on.
    Higher ELO miners get priority for their preferred issues.

    \b
    Arguments:
        ISSUE_IDS: Space-separated list of issue IDs in preference order

    \b
    Examples:
        gitt issue prefer 42 15 8
        gitt issue prefer 1 2 3 --clear
    """
    if clear:
        clear_preferences()

    if not issue_ids:
        current = load_preferences()
        if current:
            console.print(f'[cyan]Current preferences:[/cyan] {current}')
        else:
            console.print('[yellow]No preferences set. Provide issue IDs to set preferences.[/yellow]')
        console.print('\n[dim]Usage: gitt issue prefer <id1> <id2> ...[/dim]')
        return

    preferences = list(issue_ids)[:5]  # Max 5

    # Display preferences
    console.print('\n[bold]Your Ranked Preferences:[/bold]')
    for i, issue_id in enumerate(preferences, 1):
        console.print(f'  {i}. Issue #{issue_id}')

    if len(issue_ids) > 5:
        console.print(f'\n[yellow]Note: Only first 5 preferences saved (you provided {len(issue_ids)})[/yellow]')

    # Confirm and save
    if click.confirm('\nSave these preferences?', default=True):
        if save_preferences(preferences):
            console.print(f'[green]Preferences saved to {ISSUE_PREFERENCES_FILE}[/green]')
            console.print('[dim]Your miner will automatically serve these to validators.[/dim]')
            console.print('[dim]You will be assigned based on ELO priority when pairs are formed.[/dim]')
        else:
            console.print('[red]Failed to save preferences.[/red]')


@click.command('enroll')
@click.argument('issue_id', type=int)
def issue_enroll(issue_id: int):
    """
    Quick enroll for a single issue (shorthand for prefer).

    This is equivalent to running "prefer" with a single issue ID.

    \b
    Arguments:
        ISSUE_ID: The issue ID to enroll for

    \b
    Example:
        gitt issue enroll 42
    """
    current = load_preferences()
    if issue_id in current:
        console.print(f'[yellow]Already enrolled for issue #{issue_id}[/yellow]')
        console.print(f'Current preferences: {current}')
        return

    # Add to front of preferences
    new_prefs = [issue_id] + [p for p in current if p != issue_id][:4]

    if save_preferences(new_prefs):
        console.print(f'[green]Enrolled for issue #{issue_id}[/green]')
        console.print(f'New preferences: {new_prefs}')
    else:
        console.print('[red]Failed to enroll.[/red]')


@click.command('withdraw')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation prompt')
def issue_withdraw(force: bool):
    """
    Clear issue preferences (stop competing for new issues).

    This removes your local preferences file. You will no longer
    be matched for new competitions.

    NOTE: You cannot withdraw from an active competition once started.

    \b
    Example:
        gitt issue withdraw
        gitt issue withdraw --force
    """
    preferences = load_preferences()

    if not preferences:
        console.print('[yellow]No preferences to clear.[/yellow]')
        return

    console.print(f'[cyan]Current preferences:[/cyan] {preferences}')

    if force or click.confirm('\nClear all issue preferences?', default=False):
        if clear_preferences():
            console.print('[green]Preferences cleared.[/green]')
            console.print('[dim]You will not be matched for new competitions.[/dim]')
        else:
            console.print('[red]Failed to clear preferences.[/red]')
