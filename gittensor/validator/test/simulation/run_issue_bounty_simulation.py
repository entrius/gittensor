# The MIT License (MIT)
# Copyright 2025 Entrius

"""
Issue Bounty Simulation Runner

Simulates the full issue competition flow:
1. Load configuration (contract client or mock)
2. Register test issues
3. Simulate miner preferences
4. Run pairing algorithm
5. Start competitions
6. Simulate solution detection
7. Calculate ELO changes
8. Store to database and generate report

Usage:
    # Mock mode (default - no contract needed)
    python run_issue_bounty_simulation.py --mock [--store] [--miners N] [--issues N]

    # Live contract mode
    python run_issue_bounty_simulation.py --live --network localnet [--store]
    python run_issue_bounty_simulation.py --live --network testnet --contract-address 5xxx [--store]
    python run_issue_bounty_simulation.py --live --network mainnet --contract-address 5xxx [--store]

Networks:
    localnet: ws://127.0.0.1:9944 (default for local development)
    testnet:  wss://test.finney.opentensor.ai:443
    mainnet:  wss://entrypoint-finney.opentensor.ai:443
"""

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import bittensor as bt
from dotenv import load_dotenv

from gittensor.validator.issue_competitions.constants import (
    ELO_CUTOFF,
    INITIAL_ELO,
    K_FACTOR,
)
from gittensor.validator.issue_competitions.contract_client import (
    CompetitionStatus,
    ContractCompetition,
    ContractIssue,
    IssueCompetitionContractClient,
    IssueStatus,
)
from gittensor.validator.issue_competitions.elo import (
    CompetitionRecord,
    EloRating,
    calculate_elo_ema,
    get_elo_rankings,
)
from gittensor.validator.issue_competitions.pairing import (
    calculate_pairing_stats,
    find_pairs_for_issues,
)


# =============================================================================
# Path Setup
# =============================================================================

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

validator_env = Path(__file__).parent.parent.parent / '.env'
load_dotenv(validator_env)

# Enable bittensor logging to console
bt.logging.set_debug(True)


# =============================================================================
# Mock Contract Client
# =============================================================================


class MockContractClient:
    """
    In-memory mock of IssueCompetitionContractClient for simulation.

    Mirrors the contract client interface but stores all state in memory.
    """

    def __init__(self):
        self.issues: Dict[int, ContractIssue] = {}
        self.competitions: Dict[int, ContractCompetition] = {}
        self._next_issue_id = 1
        self._next_competition_id = 1
        self._current_block = 1000

    def register_issue(
        self,
        repository_full_name: str,
        issue_number: int,
        bounty_amount: int,
        target_bounty: int = 0,
    ) -> ContractIssue:
        """
        Register a new issue for competition.

        Args:
            repository_full_name: GitHub repo name (e.g., 'owner/repo')
            issue_number: GitHub issue number
            bounty_amount: Initial bounty in TAO
            target_bounty: Target bounty for full funding (0 = fully funded immediately)

        Returns:
            Registered ContractIssue
        """
        issue_id = self._next_issue_id
        self._next_issue_id += 1

        github_url = f'https://github.com/{repository_full_name}/issues/{issue_number}'
        url_hash = self._hash_url(github_url)

        is_fully_funded = target_bounty == 0 or bounty_amount >= target_bounty

        issue = ContractIssue(
            id=issue_id,
            github_url_hash=url_hash,
            repository_full_name=repository_full_name,
            issue_number=issue_number,
            bounty_amount=bounty_amount,
            target_bounty=target_bounty if target_bounty > 0 else bounty_amount,
            status=IssueStatus.ACTIVE if is_fully_funded else IssueStatus.REGISTERED,
            registered_at_block=self._current_block,
            is_fully_funded=is_fully_funded,
        )

        self.issues[issue_id] = issue
        bt.logging.debug(f'Registered issue {issue_id}: {repository_full_name}#{issue_number}')
        return issue

    def get_available_issues(self) -> List[ContractIssue]:
        """
        Get issues with ACTIVE status (ready for competition).

        Returns:
            List of active issues
        """
        return [
            issue for issue in self.issues.values()
            if issue.status == IssueStatus.ACTIVE
        ]

    def start_competition(
        self,
        issue_id: int,
        miner1_hotkey: str,
        miner2_hotkey: str,
        submission_window_blocks: int = 14400,
        deadline_blocks: int = 50400,
    ) -> Optional[ContractCompetition]:
        """
        Start a competition for an issue with two miners.

        Args:
            issue_id: Issue to start competition for
            miner1_hotkey: First miner's hotkey
            miner2_hotkey: Second miner's hotkey
            submission_window_blocks: Blocks until submission window closes
            deadline_blocks: Blocks until competition deadline

        Returns:
            Started competition or None if issue not available
        """
        issue = self.issues.get(issue_id)
        if not issue or issue.status != IssueStatus.ACTIVE:
            bt.logging.warning(f'Cannot start competition: issue {issue_id} not available')
            return None

        competition_id = self._next_competition_id
        self._next_competition_id += 1

        competition = ContractCompetition(
            id=competition_id,
            issue_id=issue_id,
            miner1_hotkey=miner1_hotkey,
            miner2_hotkey=miner2_hotkey,
            start_block=self._current_block,
            submission_window_end_block=self._current_block + submission_window_blocks,
            deadline_block=self._current_block + deadline_blocks,
            status=CompetitionStatus.ACTIVE,
        )

        self.competitions[competition_id] = competition
        issue.status = IssueStatus.IN_COMPETITION

        bt.logging.debug(
            f'Started competition {competition_id} for issue {issue_id}: '
            f'{miner1_hotkey[:8]}... vs {miner2_hotkey[:8]}...'
        )
        return competition

    def complete_competition(
        self,
        competition_id: int,
        winner_hotkey: Optional[str] = None,
        timed_out: bool = False,
    ) -> bool:
        """
        Complete a competition with a winner or timeout.

        Args:
            competition_id: Competition to complete
            winner_hotkey: Winner's hotkey (None if timed out)
            timed_out: True if competition timed out with no solution

        Returns:
            True if successfully completed
        """
        competition = self.competitions.get(competition_id)
        if not competition or competition.status != CompetitionStatus.ACTIVE:
            bt.logging.warning(f'Cannot complete competition {competition_id}: not active')
            return False

        issue = self.issues.get(competition.issue_id)

        if timed_out:
            competition.status = CompetitionStatus.TIMED_OUT
            if issue:
                issue.status = IssueStatus.ACTIVE  # Return to pool
            bt.logging.debug(f'Competition {competition_id} timed out')
        else:
            competition.status = CompetitionStatus.COMPLETED
            competition.winner_hotkey = winner_hotkey
            competition.payout_amount = issue.bounty_amount if issue else 0
            if issue:
                issue.status = IssueStatus.COMPLETED
            bt.logging.debug(f'Competition {competition_id} completed: winner={winner_hotkey[:8]}...')

        return True

    def get_active_competitions(self) -> List[ContractCompetition]:
        """Get all active competitions."""
        return [
            comp for comp in self.competitions.values()
            if comp.status == CompetitionStatus.ACTIVE
        ]

    def get_miners_in_competition(self) -> Set[str]:
        """Get set of hotkeys currently in active competitions."""
        miners = set()
        for comp in self.get_active_competitions():
            miners.add(comp.miner1_hotkey)
            miners.add(comp.miner2_hotkey)
        return miners

    def advance_blocks(self, blocks: int = 1):
        """Advance the simulated block number."""
        self._current_block += blocks

    @staticmethod
    def _hash_url(url: str) -> bytes:
        """Hash a URL for storage."""
        import hashlib
        return hashlib.sha256(url.encode()).digest()


# =============================================================================
# Simulation Data Structures
# =============================================================================


@dataclass
class SimulatedMiner:
    """Represents a miner in the simulation."""

    uid: int
    hotkey: str
    elo: int = INITIAL_ELO
    wins: int = 0
    losses: int = 0
    competitions_history: List[CompetitionRecord] = field(default_factory=list)

    @property
    def is_eligible(self) -> bool:
        return self.elo >= ELO_CUTOFF

    def to_elo_rating(self) -> EloRating:
        """Convert to EloRating for pairing algorithm."""
        return EloRating(
            uid=self.uid,
            hotkey=self.hotkey,
            elo=self.elo,
            wins=self.wins,
            losses=self.losses,
            is_eligible=self.is_eligible,
        )


@dataclass
class SimulationResult:
    """Results from running the simulation."""

    total_issues: int
    total_miners: int
    total_competitions: int
    competitions_solved: int
    competitions_timed_out: int
    final_elo_rankings: List[EloRating]
    pairing_stats: Dict


# =============================================================================
# Database Storage (for --store flag)
# =============================================================================


def create_db_connection():
    """Create database connection from env vars."""
    try:
        import psycopg2
    except ImportError:
        print('ERROR: psycopg2 not installed')
        return None

    try:
        conn = psycopg2.connect(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', 5432)),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASSWORD', ''),
            database=os.getenv('DB_NAME', 'gittensor_validator'),
        )
        conn.autocommit = False
        print(f'Connected to {os.getenv("DB_NAME", "gittensor_validator")}')
        return conn
    except Exception as e:
        print(f'ERROR: DB connection failed: {e}')
        return None


def store_elo_scores(conn, miners: List[SimulatedMiner]) -> int:
    """
    Store current ELO scores to elo_scores table.

    Args:
        conn: Database connection
        miners: List of simulated miners with ELO scores

    Returns:
        Number of records stored
    """
    from psycopg2.extras import execute_values

    query = """
    INSERT INTO elo_scores (hotkey, uid, elo, wins, losses, is_eligible, updated_at)
    VALUES %s
    ON CONFLICT (hotkey)
    DO UPDATE SET
        uid = EXCLUDED.uid,
        elo = EXCLUDED.elo,
        wins = EXCLUDED.wins,
        losses = EXCLUDED.losses,
        is_eligible = EXCLUDED.is_eligible,
        updated_at = NOW()
    """

    values = [
        (m.hotkey, m.uid, m.elo, m.wins, m.losses, m.is_eligible)
        for m in miners
    ]

    try:
        cur = conn.cursor()
        execute_values(cur, query, values)
        conn.commit()
        cur.close()
        return len(values)
    except Exception as e:
        conn.rollback()
        bt.logging.error(f'Error storing ELO scores: {e}')
        return 0


def store_elo_history(
    conn,
    hotkey: str,
    competition_id: int,
    old_elo: int,
    new_elo: int,
    is_winner: bool,
    opponent_hotkey: str,
) -> bool:
    """
    Store an ELO change record to elo_history table.

    Args:
        conn: Database connection
        hotkey: Miner's hotkey
        competition_id: Competition that caused the change
        old_elo: ELO before competition
        new_elo: ELO after competition
        is_winner: Whether miner won
        opponent_hotkey: Opponent's hotkey

    Returns:
        True if stored successfully
    """
    query = """
    INSERT INTO elo_history (hotkey, competition_id, old_elo, new_elo, elo_change, is_winner, opponent_hotkey, recorded_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
    """

    try:
        cur = conn.cursor()
        cur.execute(query, (hotkey, competition_id, old_elo, new_elo, new_elo - old_elo, is_winner, opponent_hotkey))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        conn.rollback()
        bt.logging.error(f'Error storing ELO history: {e}')
        return False


def store_competition_record(conn, competition: ContractCompetition) -> bool:
    """
    Store a competition record to competitions table.

    Args:
        conn: Database connection
        competition: Competition to store

    Returns:
        True if stored successfully
    """
    query = """
    INSERT INTO competitions (id, issue_id, miner1_hotkey, miner2_hotkey, status, winner_hotkey, payout_amount, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (id)
    DO UPDATE SET
        status = EXCLUDED.status,
        winner_hotkey = EXCLUDED.winner_hotkey,
        payout_amount = EXCLUDED.payout_amount
    """

    try:
        cur = conn.cursor()
        cur.execute(query, (
            competition.id,
            competition.issue_id,
            competition.miner1_hotkey,
            competition.miner2_hotkey,
            competition.status.name,
            competition.winner_hotkey,
            competition.payout_amount,
        ))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        conn.rollback()
        bt.logging.error(f'Error storing competition: {e}')
        return False


def store_issue_bounty(conn, issue: ContractIssue) -> bool:
    """
    Store an issue bounty record to issue_bounties table.

    Args:
        conn: Database connection
        issue: Issue to store

    Returns:
        True if stored successfully
    """
    query = """
    INSERT INTO issue_bounties (id, repository_full_name, issue_number, bounty_amount, status, created_at)
    VALUES (%s, %s, %s, %s, %s, NOW())
    ON CONFLICT (id)
    DO UPDATE SET
        bounty_amount = EXCLUDED.bounty_amount,
        status = EXCLUDED.status
    """

    try:
        cur = conn.cursor()
        cur.execute(query, (
            issue.id,
            issue.repository_full_name,
            issue.issue_number,
            issue.bounty_amount,
            issue.status.name,
        ))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        conn.rollback()
        bt.logging.error(f'Error storing issue bounty: {e}')
        return False


# =============================================================================
# Simulation Logic
# =============================================================================


def generate_mock_hotkey(uid: int) -> str:
    """Generate a mock SS58-like hotkey for testing."""
    import hashlib
    hash_bytes = hashlib.sha256(f'miner_{uid}'.encode()).hexdigest()[:48]
    return f'5{hash_bytes}'


def create_mock_miners(num_miners: int) -> List[SimulatedMiner]:
    """
    Create mock miners with randomized initial ELO.

    Args:
        num_miners: Number of miners to create

    Returns:
        List of SimulatedMiner objects
    """
    miners = []
    for uid in range(num_miners):
        # Randomize initial ELO around 800 (+/- 100)
        initial_elo = random.randint(700, 900)
        hotkey = generate_mock_hotkey(uid)

        miner = SimulatedMiner(
            uid=uid,
            hotkey=hotkey,
            elo=initial_elo,
        )
        miners.append(miner)

    return miners


def create_mock_issues(client: MockContractClient, num_issues: int) -> List[ContractIssue]:
    """
    Create mock issues with various bounty amounts.

    Args:
        client: MockContractClient to register issues with
        num_issues: Number of issues to create

    Returns:
        List of registered ContractIssue objects
    """
    repos = [
        'opentensor/bittensor',
        'opentensor/subtensor',
        'entrius/gittensor',
        'huggingface/transformers',
        'pytorch/pytorch',
    ]

    issues = []
    for i in range(num_issues):
        repo = repos[i % len(repos)]
        issue_number = 100 + i
        bounty = random.choice([100, 250, 500, 1000, 2500])  # TAO bounty

        issue = client.register_issue(
            repository_full_name=repo,
            issue_number=issue_number,
            bounty_amount=bounty,
        )
        issues.append(issue)

    return issues


def generate_miner_preferences(
    miners: List[SimulatedMiner],
    issues: List[ContractIssue],
) -> Dict[str, List[int]]:
    """
    Generate random miner preferences for issues.

    Each miner picks 1-3 random issues they're interested in.

    Args:
        miners: List of miners
        issues: List of available issues

    Returns:
        Dict mapping hotkey -> list of issue IDs (preference order)
    """
    preferences: Dict[str, List[int]] = {}

    for miner in miners:
        # Each miner picks 1-3 issues
        num_prefs = random.randint(1, min(3, len(issues)))
        selected_issues = random.sample(issues, num_prefs)
        preferences[miner.hotkey] = [issue.id for issue in selected_issues]

    return preferences


def simulate_competition_outcome(
    competition: ContractCompetition,
    miners_by_hotkey: Dict[str, SimulatedMiner],
) -> Tuple[Optional[str], bool]:
    """
    Simulate the outcome of a competition.

    70% chance of solution (random winner), 30% chance of timeout.

    Args:
        competition: The competition to simulate
        miners_by_hotkey: Dict mapping hotkey -> SimulatedMiner

    Returns:
        Tuple of (winner_hotkey or None, timed_out)
    """
    if random.random() < 0.70:
        # Competition solved - random winner
        winner = random.choice([competition.miner1_hotkey, competition.miner2_hotkey])
        return winner, False
    else:
        # Competition timed out
        return None, True


def apply_elo_changes(
    competition: ContractCompetition,
    winner_hotkey: Optional[str],
    miners_by_hotkey: Dict[str, SimulatedMiner],
    now: datetime,
) -> Dict[str, Tuple[int, int]]:
    """
    Apply ELO changes based on competition outcome.

    Args:
        competition: Completed competition
        winner_hotkey: Winner's hotkey (None if timeout)
        miners_by_hotkey: Dict mapping hotkey -> SimulatedMiner
        now: Current timestamp

    Returns:
        Dict mapping hotkey -> (old_elo, new_elo) for both miners
    """
    miner1 = miners_by_hotkey.get(competition.miner1_hotkey)
    miner2 = miners_by_hotkey.get(competition.miner2_hotkey)

    if not miner1 or not miner2:
        return {}

    changes = {}

    if winner_hotkey is None:
        # Timeout - no ELO changes
        return changes

    # Determine winner/loser
    winner = miner1 if winner_hotkey == miner1.hotkey else miner2
    loser = miner2 if winner == miner1 else miner1

    # Record old ELO
    winner_old_elo = winner.elo
    loser_old_elo = loser.elo

    # Add competition records for EMA calculation
    winner.competitions_history.append(
        CompetitionRecord(
            competition_id=competition.id,
            completed_at=now,
            is_winner=True,
            opponent_elo=loser.elo,
            bounty_amount=competition.payout_amount or 0,
        )
    )

    loser.competitions_history.append(
        CompetitionRecord(
            competition_id=competition.id,
            completed_at=now,
            is_winner=False,
            opponent_elo=winner.elo,
            bounty_amount=competition.payout_amount or 0,
        )
    )

    # Recalculate ELO using EMA
    winner.elo = calculate_elo_ema(winner.competitions_history, now)
    loser.elo = calculate_elo_ema(loser.competitions_history, now)

    # Update win/loss counts
    winner.wins += 1
    loser.losses += 1

    changes[winner.hotkey] = (winner_old_elo, winner.elo)
    changes[loser.hotkey] = (loser_old_elo, loser.elo)

    return changes


# =============================================================================
# Main Simulation
# =============================================================================


# Network configurations
NETWORK_CONFIGS = {
    'localnet': {
        'ws_endpoint': 'ws://127.0.0.1:9944',
        'contract_address': None,  # Must be provided or auto-detected
    },
    'testnet': {
        'ws_endpoint': 'wss://test.finney.opentensor.ai:443',
        'contract_address': None,  # Must be provided
    },
    'mainnet': {
        'ws_endpoint': 'wss://entrypoint-finney.opentensor.ai:443',
        'contract_address': None,  # Must be provided
    },
}


def run_issue_bounty_simulation(
    num_miners: int = 10,
    num_issues: int = 5,
    store_results: bool = False,
    live_mode: bool = False,
    network: str = 'localnet',
    contract_address: Optional[str] = None,
    ws_endpoint: Optional[str] = None,
) -> SimulationResult:
    """
    Run the full issue bounty competition simulation.

    Args:
        num_miners: Number of mock miners to create (mock mode only)
        num_issues: Number of mock issues to create (mock mode only)
        store_results: If True, store results to database
        live_mode: If True, use real contract instead of mock
        network: Network to connect to (localnet, testnet, mainnet)
        contract_address: Override contract address
        ws_endpoint: Override WebSocket endpoint

    Returns:
        SimulationResult with all metrics
    """
    mode_str = f'LIVE ({network.upper()})' if live_mode else 'MOCK'
    print('=' * 70)
    print(f'ISSUE BOUNTY SIMULATION START [{mode_str}]')
    print('=' * 70)
    sys.stdout.flush()
    time.sleep(0.1)

    # Database connection (for storage)
    conn = None
    if store_results:
        conn = create_db_connection()

    # Step 1: Initialize contract client (mock or live)
    if live_mode:
        print(f'\n[1/8] Initializing LIVE contract client ({network})...')
        sys.stdout.flush()
        time.sleep(0.1)

        # Get network config
        config = NETWORK_CONFIGS.get(network, NETWORK_CONFIGS['localnet'])
        endpoint = ws_endpoint or config['ws_endpoint']
        address = contract_address or config['contract_address']

        if not address:
            # Try to read from environment
            address = os.getenv('CONTRACT_ADDRESS')

        if not address:
            print('  ERROR: Contract address required for live mode')
            print('  Use --contract-address or set CONTRACT_ADDRESS env var')
            sys.exit(1)

        print(f'  WS Endpoint: {endpoint}')
        print(f'  Contract: {address}')

        # Connect to subtensor
        subtensor = bt.Subtensor(network=endpoint)
        client = IssueCompetitionContractClient(
            contract_address=address,
            subtensor=subtensor,
        )
        print('  Live contract client ready')
    else:
        print('\n[1/8] Initializing MOCK contract client...')
        sys.stdout.flush()
        time.sleep(0.1)
        client = MockContractClient()
        print('  Mock contract client ready')

    # Step 2: Register test issues
    print(f'\n[2/8] Registering {num_issues} test issues...')
    sys.stdout.flush()
    time.sleep(0.1)
    issues = create_mock_issues(client, num_issues)
    for issue in issues:
        print(f'  Issue {issue.id}: {issue.repository_full_name}#{issue.issue_number} - {issue.bounty_amount} TAO')
        if store_results and conn:
            store_issue_bounty(conn, issue)

    # Step 3: Create mock miners
    print(f'\n[3/8] Creating {num_miners} mock miners...')
    sys.stdout.flush()
    time.sleep(0.1)
    miners = create_mock_miners(num_miners)
    miners_by_hotkey = {m.hotkey: m for m in miners}
    print(f'  Created {len(miners)} miners with ELO range {min(m.elo for m in miners)}-{max(m.elo for m in miners)}')

    # Step 4: Generate miner preferences
    print('\n[4/8] Generating miner preferences...')
    sys.stdout.flush()
    time.sleep(0.1)
    preferences = generate_miner_preferences(miners, issues)
    for hotkey, issue_ids in list(preferences.items())[:5]:
        miner = miners_by_hotkey[hotkey]
        print(f'  Miner UID {miner.uid}: interested in issues {issue_ids}')
    if len(preferences) > 5:
        print(f'  ... and {len(preferences) - 5} more miners')

    # Step 5: Run pairing algorithm
    print('\n[5/8] Running pairing algorithm...')
    sys.stdout.flush()
    time.sleep(0.1)

    # Convert miners to EloRating dict for pairing
    elo_ratings = {m.hotkey: m.to_elo_rating() for m in miners}
    available_issues = client.get_available_issues()
    miners_in_competition = client.get_miners_in_competition()

    pairs = find_pairs_for_issues(
        available_issues=available_issues,
        miner_preferences=preferences,
        elo_ratings=elo_ratings,
        miners_in_competition=miners_in_competition,
    )

    pairing_stats = calculate_pairing_stats(pairs, elo_ratings)
    print(f'  Found {len(pairs)} pairs')
    print(f'  Avg ELO diff: {pairing_stats["avg_elo_diff"]:.1f}')

    # Step 6: Start competitions and simulate outcomes
    print('\n[6/8] Starting competitions and simulating outcomes...')
    sys.stdout.flush()
    time.sleep(0.1)

    now = datetime.now(timezone.utc)
    competitions_solved = 0
    competitions_timed_out = 0
    all_elo_changes: List[Dict] = []

    for issue_id, miner1_hotkey, miner2_hotkey in pairs:
        # Start competition
        competition = client.start_competition(issue_id, miner1_hotkey, miner2_hotkey)
        if not competition:
            continue

        # Simulate outcome
        winner, timed_out = simulate_competition_outcome(competition, miners_by_hotkey)

        # Complete competition
        client.complete_competition(competition.id, winner, timed_out)

        if timed_out:
            competitions_timed_out += 1
            outcome_str = 'TIMEOUT'
        else:
            competitions_solved += 1
            winner_miner = miners_by_hotkey.get(winner)
            outcome_str = f'Winner: UID {winner_miner.uid if winner_miner else "?"}'

        print(f'  Competition {competition.id} (Issue {issue_id}): {outcome_str}')

        # Store competition record
        if store_results and conn:
            store_competition_record(conn, competition)

    # Step 7: Calculate ELO changes
    print('\n[7/8] Calculating ELO changes...')
    sys.stdout.flush()
    time.sleep(0.1)

    # Re-process completed competitions for ELO
    for comp in client.competitions.values():
        if comp.status == CompetitionStatus.COMPLETED:
            changes = apply_elo_changes(comp, comp.winner_hotkey, miners_by_hotkey, now)
            for hotkey, (old_elo, new_elo) in changes.items():
                miner = miners_by_hotkey[hotkey]
                change = new_elo - old_elo
                print(f'  Miner UID {miner.uid}: {old_elo} -> {new_elo} ({change:+d})')

                # Store ELO history
                if store_results and conn:
                    opponent = comp.miner2_hotkey if hotkey == comp.miner1_hotkey else comp.miner1_hotkey
                    is_winner = hotkey == comp.winner_hotkey
                    store_elo_history(conn, hotkey, comp.id, old_elo, new_elo, is_winner, opponent)

    # Step 8: Generate report and store final ELO
    print('\n[8/8] Generating final report...')
    sys.stdout.flush()
    time.sleep(0.1)

    # Store final ELO scores
    if store_results and conn:
        stored = store_elo_scores(conn, miners)
        print(f'  Stored {stored} ELO scores to database')
        conn.close()

    # Build final rankings
    miner_info = {m.hotkey: {'uid': m.uid} for m in miners}
    miner_competitions = {m.hotkey: m.competitions_history for m in miners}
    final_rankings = get_elo_rankings(miner_competitions, miner_info, now)

    # Print summary report
    _print_summary_report(
        miners=miners,
        issues=issues,
        pairs=pairs,
        competitions_solved=competitions_solved,
        competitions_timed_out=competitions_timed_out,
        final_rankings=final_rankings,
        pairing_stats=pairing_stats,
    )

    print('\n' + '=' * 70)
    print('ISSUE BOUNTY SIMULATION COMPLETE')
    print('=' * 70)

    return SimulationResult(
        total_issues=len(issues),
        total_miners=len(miners),
        total_competitions=len(pairs),
        competitions_solved=competitions_solved,
        competitions_timed_out=competitions_timed_out,
        final_elo_rankings=final_rankings,
        pairing_stats=pairing_stats,
    )


def _print_summary_report(
    miners: List[SimulatedMiner],
    issues: List[ContractIssue],
    pairs: List[Tuple[int, str, str]],
    competitions_solved: int,
    competitions_timed_out: int,
    final_rankings: List[EloRating],
    pairing_stats: Dict,
) -> None:
    """Print comprehensive summary report."""
    print('\n' + '=' * 70)
    print('SIMULATION SUMMARY REPORT')
    print('=' * 70)

    # Overview
    print('\n[1/4] OVERVIEW')
    print('-' * 70)
    print(f'  Total Issues:        {len(issues)}')
    print(f'  Total Miners:        {len(miners)}')
    print(f'  Total Competitions:  {len(pairs)}')
    print(f'  Solved:              {competitions_solved} ({competitions_solved / max(len(pairs), 1) * 100:.1f}%)')
    print(f'  Timed Out:           {competitions_timed_out} ({competitions_timed_out / max(len(pairs), 1) * 100:.1f}%)')

    # Pairing Stats
    print('\n[2/4] PAIRING STATISTICS')
    print('-' * 70)
    print(f'  Pairs Created:       {pairing_stats["total_pairs"]}')
    print(f'  Avg ELO Difference:  {pairing_stats["avg_elo_diff"]:.1f}')
    print(f'  Max ELO Difference:  {pairing_stats["max_elo_diff"]}')
    print(f'  Min ELO Difference:  {pairing_stats["min_elo_diff"]}')

    # ELO Rankings
    print('\n[3/4] FINAL ELO RANKINGS (Top 10)')
    print('-' * 70)
    print(f'{"Rank":<6} {"UID":<6} {"ELO":<8} {"W-L":<10} {"Eligible":<10}')
    print('-' * 70)

    for i, rating in enumerate(final_rankings[:10], 1):
        wl = f'{rating.wins}-{rating.losses}'
        eligible = 'Yes' if rating.is_eligible else 'No'
        print(f'{i:<6} {rating.uid:<6} {rating.elo:<8} {wl:<10} {eligible:<10}')

    # Issue Status
    print('\n[4/4] ISSUE STATUS')
    print('-' * 70)
    for issue in issues:
        print(f'  Issue {issue.id}: {issue.repository_full_name}#{issue.issue_number} - {issue.status.name}')

    print('\n' + '=' * 70)


# =============================================================================
# Entry Point
# =============================================================================


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Run issue bounty competition simulation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Mock mode (no contract needed)
    python run_issue_bounty_simulation.py --mock
    python run_issue_bounty_simulation.py --mock --miners 20 --issues 10

    # Live contract mode
    python run_issue_bounty_simulation.py --live --network localnet
    python run_issue_bounty_simulation.py --live --network testnet --contract-address 5xxx
    python run_issue_bounty_simulation.py --live --network mainnet --contract-address 5xxx

    # Store results to database
    python run_issue_bounty_simulation.py --mock --store
    python run_issue_bounty_simulation.py --live --network localnet --store
        """,
    )

    # Mode selection (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--mock',
        action='store_true',
        default=True,
        help='Use mock contract client (default)',
    )
    mode_group.add_argument(
        '--live',
        action='store_true',
        help='Use live contract on blockchain',
    )

    # Network options (for live mode)
    parser.add_argument(
        '--network',
        type=str,
        choices=['localnet', 'testnet', 'mainnet'],
        default='localnet',
        help='Network to connect to (default: localnet)',
    )
    parser.add_argument(
        '--contract-address',
        type=str,
        help='Smart contract address (required for testnet/mainnet)',
    )
    parser.add_argument(
        '--ws-endpoint',
        type=str,
        help='Override WebSocket endpoint',
    )

    # Simulation options
    parser.add_argument(
        '--store',
        action='store_true',
        help='Store results to database',
    )
    parser.add_argument(
        '--miners',
        type=int,
        default=10,
        help='Number of mock miners (mock mode only, default: 10)',
    )
    parser.add_argument(
        '--issues',
        type=int,
        default=5,
        help='Number of mock issues (mock mode only, default: 5)',
    )

    args = parser.parse_args()

    # Validate live mode requirements
    if args.live:
        if args.network in ['testnet', 'mainnet'] and not args.contract_address:
            if not os.getenv('CONTRACT_ADDRESS'):
                parser.error(f'--contract-address required for {args.network}')

    try:
        run_issue_bounty_simulation(
            num_miners=args.miners,
            num_issues=args.issues,
            store_results=args.store,
            live_mode=args.live,
            network=args.network,
            contract_address=args.contract_address,
            ws_endpoint=args.ws_endpoint,
        )
    except KeyboardInterrupt:
        print('\nInterrupted')
        sys.exit(0)
    except Exception as e:
        print(f'ERROR: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
