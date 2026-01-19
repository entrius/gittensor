# The MIT License (MIT)
# Copyright 2025 Entrius

"""Client for interacting with the Issues Competition smart contract."""

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

import bittensor as bt

try:
    from substrateinterface import Keypair
    from substrateinterface.contracts import ContractInstance

    SUBSTRATE_INTERFACE_AVAILABLE = True
except ImportError:
    SUBSTRATE_INTERFACE_AVAILABLE = False
    ContractInstance = None
    Keypair = None

# Default path to contract metadata file (ink! Rust contract)
DEFAULT_CONTRACT_METADATA_PATH = Path(__file__).parent.parent.parent.parent.parent / \
    'smart-contracts' / 'ink' / 'target' / 'ink' / 'issue_bounty_manager.contract'

# Default gas limits for contract calls
DEFAULT_GAS_LIMIT = {
    'ref_time': 10_000_000_000,  # 10 billion
    'proof_size': 500_000,  # 500 KB
}

# Config file path for local dev environment
GITTENSOR_CONFIG_PATH = Path.home() / '.gittensor' / 'contract_config.json'


def get_contract_address_from_config() -> Optional[str]:
    """
    Get contract address from environment variable or local config file.

    Priority:
    1. CONTRACT_ADDRESS environment variable
    2. ~/.gittensor/contract_config.json (written by dev-environment up.sh)
    3. Return None (caller should use constants as fallback)

    Returns:
        Contract address string or None
    """
    # 1. Environment variable (highest priority)
    env_addr = os.environ.get('CONTRACT_ADDRESS')
    if env_addr:
        bt.logging.debug(f'Using contract address from env: {env_addr[:20]}...')
        return env_addr

    # 2. Local config file (for dev environment)
    if GITTENSOR_CONFIG_PATH.exists():
        try:
            with open(GITTENSOR_CONFIG_PATH) as f:
                config = json.load(f)
                addr = config.get('contract_address')
                if addr:
                    bt.logging.debug(f'Using contract address from config: {addr[:20]}...')
                    return addr
        except (json.JSONDecodeError, IOError) as e:
            bt.logging.warning(f'Failed to read contract config: {e}')

    return None


class IssueStatus(Enum):
    """Status of an issue in its lifecycle."""

    REGISTERED = 0
    ACTIVE = 1
    IN_COMPETITION = 2
    COMPLETED = 3
    CANCELLED = 4


class CompetitionStatus(Enum):
    """Status of a competition."""

    ACTIVE = 0
    COMPLETED = 1
    TIMED_OUT = 2
    CANCELLED = 3


@dataclass
class ContractIssue:
    """Issue data from the smart contract."""

    id: int
    github_url_hash: bytes
    repository_full_name: str
    issue_number: int
    bounty_amount: int
    target_bounty: int
    status: IssueStatus
    registered_at_block: int
    is_fully_funded: bool


@dataclass
class ContractCompetition:
    """Competition data from the smart contract."""

    id: int
    issue_id: int
    miner1_hotkey: str
    miner2_hotkey: str
    start_block: int
    submission_window_end_block: int
    deadline_block: int
    status: CompetitionStatus
    winner_hotkey: Optional[str] = None
    winning_pr_url_hash: Optional[bytes] = None
    payout_amount: Optional[int] = None


@dataclass
class PairProposal:
    """A proposal to pair two miners for a competition."""

    issue_id: int
    miner1_hotkey: str
    miner2_hotkey: str
    proposer: str
    proposed_at_block: int
    total_stake_voted: int


class IssueCompetitionContractClient:
    """
    Client for interacting with the Issues Competition smart contract.

    This client handles all read/write operations with the on-chain contract
    for the issue competitions sub-mechanism.
    """

    def __init__(
        self,
        contract_address: Optional[str] = None,
        subtensor: Optional[bt.Subtensor] = None,
        metadata_path: Optional[Path] = None,
    ):
        """
        Initialize the contract client.

        Args:
            contract_address: Address of the deployed contract (optional, will check env/config)
            subtensor: Bittensor subtensor instance for chain interaction
            metadata_path: Path to the .contract metadata file (optional)
        """
        # Try to get contract address from various sources
        if contract_address:
            self.contract_address = contract_address
        else:
            # Check env var and config file
            config_addr = get_contract_address_from_config()
            if config_addr:
                self.contract_address = config_addr
            else:
                # Fall back to constants (may be empty for testnet/mainnet)
                try:
                    from gittensor.validator.issue_competitions.constants import (
                        ISSUE_CONTRACT_ADDRESS_TESTNET,
                    )
                    self.contract_address = ISSUE_CONTRACT_ADDRESS_TESTNET
                except ImportError:
                    self.contract_address = ''

        self.subtensor = subtensor
        self.metadata_path = metadata_path or DEFAULT_CONTRACT_METADATA_PATH
        self._contract = None
        self._initialized = False

        if not self.contract_address:
            bt.logging.warning('Issue competition contract address not set')

    def _ensure_contract(self) -> bool:
        """
        Ensure contract connection is established.

        Returns:
            True if contract is ready, False otherwise
        """
        if not self.contract_address:
            bt.logging.warning('Cannot connect: contract address not set')
            return False

        if not SUBSTRATE_INTERFACE_AVAILABLE:
            bt.logging.warning(
                'substrate-interface library not available. '
                'Install with: pip install substrate-interface'
            )
            return False

        # Initialize contract if not already done
        if self._contract is None and not self._initialized:
            self._initialized = True
            try:
                # Check if metadata file exists
                if not self.metadata_path.exists():
                    bt.logging.warning(
                        f'Contract metadata not found at {self.metadata_path}. '
                        'Contract may not be compiled yet.'
                    )
                    return False

                # Create contract instance from address and metadata
                self._contract = ContractInstance.create_from_address(
                    contract_address=self.contract_address,
                    metadata_file=str(self.metadata_path),
                    substrate=self.subtensor.substrate,
                )
                bt.logging.info(
                    f'Connected to contract at {self.contract_address}'
                )
            except Exception as e:
                bt.logging.error(f'Failed to initialize contract: {e}')
                self._contract = None
                return False

        return self._contract is not None

    def _raw_contract_read(self, method_name: str, args: dict = None) -> Optional[bytes]:
        """
        Read from contract using raw RPC call.

        This is a workaround for substrate-interface Ink! 5 incompatibility.
        Uses state_call to ContractsApi_call and extracts raw result bytes.

        Args:
            method_name: Contract method name (e.g., 'next_issue_id')
            args: Optional method arguments

        Returns:
            Raw return data bytes, or None on error
        """
        if not self._ensure_contract():
            return None

        try:
            # Get method selector from metadata
            metadata_path = self.metadata_path.with_suffix('.json')
            if not metadata_path.exists():
                metadata_path = self.metadata_path  # Try .contract file

            with open(metadata_path) as f:
                metadata = json.load(f)

            selector = None
            for msg in metadata.get('spec', {}).get('messages', []):
                if msg['label'] == method_name:
                    selector = bytes.fromhex(msg['selector'].replace('0x', ''))
                    break

            if not selector:
                bt.logging.error(f'Method {method_name} not found in contract metadata')
                return None

            # Build input data (selector + encoded args)
            # For now, we only support no-arg methods
            input_data = selector

            # Build ContractsApi_call params
            # origin (32 bytes) + dest (32 bytes) + value (16 bytes) + gas_limit (1 byte None) + storage_limit (1 byte None) + input_data (compact Vec)
            from substrateinterface import Keypair
            caller = Keypair.create_from_uri('//Alice')

            origin = bytes.fromhex(self.subtensor.substrate.ss58_decode(caller.ss58_address))
            dest = bytes.fromhex(self.subtensor.substrate.ss58_decode(self.contract_address))
            value = b'\x00' * 16  # 0 balance
            gas_limit = b'\x00'  # None
            storage_limit = b'\x00'  # None

            # Compact encode input_data length
            data_len = len(input_data)
            if data_len < 64:
                compact_len = bytes([data_len << 2])
            else:
                compact_len = bytes([(data_len << 2) | 1, data_len >> 6])

            call_params = origin + dest + value + gas_limit + storage_limit + compact_len + input_data

            # Make state_call
            result = self.subtensor.substrate.rpc_request(
                'state_call',
                ['ContractsApi_call', '0x' + call_params.hex()]
            )

            if not result.get('result'):
                return None

            raw = bytes.fromhex(result['result'].replace('0x', ''))

            # Parse response to extract return data
            # Structure: gas_consumed(16) + more_data... + return_value
            if len(raw) < 32:
                return None

            # For simple u32 returns, the value is typically at offset 27
            # For more complex returns, we'd need full SCALE decoding
            # Return the raw bytes after gas info for caller to decode
            return raw[16:]

        except Exception as e:
            bt.logging.debug(f'Raw contract read failed: {e}')
            return None

    def _extract_u32_from_response(self, response_bytes: bytes) -> Optional[int]:
        """Extract u32 value from Ink! 5 contract response bytes."""
        if not response_bytes or len(response_bytes) < 15:
            return None

        # For simple u32 returns in Ink! 5, value is at offset 11 in the response after gas
        # (offset 27 from raw, minus 16 for gas = 11)
        try:
            import struct
            return struct.unpack_from('<I', response_bytes, 11)[0]
        except Exception:
            return None

    @staticmethod
    def hash_url(url: str) -> bytes:
        """
        Hash a GitHub URL for deduplication (matches contract implementation).

        Args:
            url: GitHub issue URL

        Returns:
            32-byte hash of the URL
        """
        return hashlib.sha256(url.encode()).digest()

    @staticmethod
    def hash_pr_url(pr_url: str) -> bytes:
        """
        Hash a PR URL for on-chain storage.

        Args:
            pr_url: GitHub PR URL

        Returns:
            32-byte hash of the PR URL
        """
        return hashlib.sha256(pr_url.encode()).digest()

    # =========================================================================
    # Query Functions (Read-only)
    # =========================================================================

    def get_available_issues(self) -> List[ContractIssue]:
        """
        Query contract for issues with status=Active (ready for competition).

        Returns:
            List of active issues available for competition
        """
        if not self._ensure_contract():
            bt.logging.debug('Contract not ready, returning empty issue list')
            return []

        try:
            # ink! contract uses get_issues_by_status with Active status
            # IssueStatus::Active = 1 in the enum
            result = self._contract.read(
                self.subtensor.substrate,
                'get_issues_by_status',
                args={'status': {'Active': None}},
            )
            if result.contract_result_data is None:
                return []

            issues = []
            for issue_data in result.contract_result_data:
                issues.append(self._parse_issue(issue_data))
            return issues
        except Exception as e:
            bt.logging.error(f'Error fetching available issues: {e}')
            return []

    def get_issue(self, issue_id: int) -> Optional[ContractIssue]:
        """
        Get a specific issue by ID.

        Args:
            issue_id: The issue ID to query

        Returns:
            Issue data if found, None otherwise
        """
        if not self._ensure_contract():
            return None

        try:
            result = self._contract.read(
                self.subtensor.substrate,
                'get_issue',
                args={'issue_id': issue_id},
            )
            if result.contract_result_data is None:
                return None
            return self._parse_issue(result.contract_result_data)
        except Exception as e:
            bt.logging.error(f'Error fetching issue {issue_id}: {e}')
            return None

    def is_miner_in_competition(self, miner_hotkey: str) -> bool:
        """
        Check if a miner is currently in an active competition.

        Args:
            miner_hotkey: Miner's hotkey (SS58 address)

        Returns:
            True if miner is in an active competition
        """
        if not self._ensure_contract():
            return False

        try:
            result = self._contract.read(
                self.subtensor.substrate,
                'is_miner_in_competition',
                args={'hotkey': miner_hotkey},
            )
            return bool(result.contract_result_data)
        except Exception as e:
            bt.logging.error(f'Error checking miner competition status: {e}')
            return False

    def get_active_competitions(self) -> List[ContractCompetition]:
        """
        Get all active competitions.

        Returns:
            List of active competitions
        """
        if not self._ensure_contract():
            return []

        try:
            result = self._contract.read(
                self.subtensor.substrate,
                'get_active_competitions',
            )
            if result.contract_result_data is None:
                return []

            competitions = []
            for comp_data in result.contract_result_data:
                competitions.append(self._parse_competition(comp_data))
            return competitions
        except Exception as e:
            bt.logging.error(f'Error fetching active competitions: {e}')
            return []

    def get_competition(self, competition_id: int) -> Optional[ContractCompetition]:
        """
        Get a specific competition by ID.

        Args:
            competition_id: The competition ID to query

        Returns:
            Competition data if found, None otherwise
        """
        if not self._ensure_contract():
            return None

        try:
            result = self._contract.read(
                self.subtensor.substrate,
                'get_competition',
                args={'competition_id': competition_id},
            )
            if result.contract_result_data is None:
                return None
            return self._parse_competition(result.contract_result_data)
        except Exception as e:
            bt.logging.error(f'Error fetching competition {competition_id}: {e}')
            return None

    def get_pair_proposal(self, issue_id: int) -> Optional[PairProposal]:
        """
        Get the active pair proposal for an issue.

        Args:
            issue_id: The issue ID

        Returns:
            Active proposal if exists, None otherwise
        """
        if not self._ensure_contract():
            return None

        try:
            result = self._contract.read(
                self.subtensor.substrate,
                'get_pair_proposal',
                args={'issue_id': issue_id},
            )
            if result.contract_result_data is None:
                return None
            return self._parse_pair_proposal(result.contract_result_data)
        except Exception as e:
            bt.logging.error(f'Error fetching pair proposal for issue {issue_id}: {e}')
            return None

    def get_alpha_pool(self) -> int:
        """
        Get the current alpha pool balance.

        Returns:
            Unallocated ALPHA in the pool (0 if contract not ready)
        """
        if not self._ensure_contract():
            return 0

        try:
            result = self._contract.read(
                self.subtensor.substrate,
                'get_alpha_pool',
            )
            return int(result.contract_result_data or 0)
        except Exception as e:
            bt.logging.error(f'Error fetching alpha pool: {e}')
            return 0

    # =========================================================================
    # Transaction Functions (Write)
    # =========================================================================

    def propose_pair(
        self,
        issue_id: int,
        miner1_hotkey: str,
        miner2_hotkey: str,
        wallet: bt.wallet,
    ) -> bool:
        """
        Propose a miner pair for competition.

        Creates a new pair proposal or votes on an existing one if the same
        pair is proposed. Requires stake to vote.

        Args:
            issue_id: Issue to start competition for
            miner1_hotkey: First miner's hotkey
            miner2_hotkey: Second miner's hotkey
            wallet: Validator wallet for signing

        Returns:
            True if proposal/vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot propose pair: contract not ready')
            return False

        try:
            bt.logging.info(
                f'Proposing pair for issue {issue_id}: '
                f'{miner1_hotkey[:8]}... vs {miner2_hotkey[:8]}...'
            )

            # Create keypair from wallet hotkey
            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='propose_pair',
                args={
                    'issue_id': issue_id,
                    'miner1_hotkey': miner1_hotkey,
                    'miner2_hotkey': miner2_hotkey,
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Pair proposal succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Pair proposal failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error proposing pair: {e}')
            return False

    def vote_pair(self, issue_id: int, wallet: bt.wallet) -> bool:
        """
        Vote on an existing pair proposal.

        Adds the caller's stake-weighted vote to the proposal. If consensus
        (51%) is reached, the competition starts automatically.

        Args:
            issue_id: Issue with active proposal
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote pair: contract not ready')
            return False

        try:
            bt.logging.info(f'Voting on pair proposal for issue {issue_id}')

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_pair',
                args={'issue_id': issue_id},
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote pair succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote pair failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting on pair: {e}')
            return False

    def vote_solution(
        self,
        competition_id: int,
        winner_hotkey: str,
        pr_url: str,
        wallet: bt.wallet,
    ) -> bool:
        """
        Vote for a competition winner.

        Casts a stake-weighted vote for the proposed winner. If consensus
        (51%) is reached, the competition completes and bounty is paid.

        Args:
            competition_id: Competition to vote on
            winner_hotkey: Hotkey of the proposed winner
            pr_url: URL of the winning PR (will be hashed)
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote solution: contract not ready')
            return False

        try:
            pr_url_hash = self.hash_pr_url(pr_url)
            bt.logging.info(
                f'Voting solution for competition {competition_id}: '
                f'winner={winner_hotkey[:8]}...'
            )

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_solution',
                args={
                    'competition_id': competition_id,
                    'winner_hotkey': winner_hotkey,
                    'pr_url_hash': list(pr_url_hash),  # Convert bytes to list for SCALE encoding
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote solution succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote solution failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting solution: {e}')
            return False

    def vote_timeout(self, competition_id: int, wallet: bt.wallet) -> bool:
        """
        Vote to timeout a competition that has passed its deadline.

        Casts a stake-weighted vote to timeout. If consensus (51%) is reached,
        the competition is cancelled and bounty returns to pool.

        Args:
            competition_id: Competition to timeout
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote timeout: contract not ready')
            return False

        try:
            bt.logging.info(f'Voting timeout for competition {competition_id}')

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_timeout',
                args={'competition_id': competition_id},
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote timeout succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote timeout failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting timeout: {e}')
            return False

    def vote_cancel(
        self,
        competition_id: int,
        reason: str,
        wallet: bt.wallet,
    ) -> bool:
        """
        Vote to cancel a competition (e.g., external solution detected).

        Args:
            competition_id: Competition to cancel
            reason: Reason for cancellation
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote cancel: contract not ready')
            return False

        try:
            reason_hash = hashlib.sha256(reason.encode()).digest()
            bt.logging.info(
                f'Voting cancel for competition {competition_id}: {reason}'
            )

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_cancel',
                args={
                    'competition_id': competition_id,
                    'reason_hash': list(reason_hash),  # Convert bytes to list for SCALE encoding
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote cancel succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote cancel failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting cancel: {e}')
            return False

    # =========================================================================
    # Helper Functions
    # =========================================================================

    def _parse_issue(self, raw_data: dict) -> ContractIssue:
        """Parse raw contract data into ContractIssue."""
        # ink! contract uses snake_case keys
        github_url_hash = raw_data.get('github_url_hash')
        if github_url_hash is None:
            github_url_hash = raw_data.get('githubUrlHash', b'\x00' * 32)
        if isinstance(github_url_hash, str):
            hex_str = github_url_hash.replace('0x', '').replace('0X', '')
            github_url_hash = bytes.fromhex(hex_str) if hex_str else b'\x00' * 32
        elif isinstance(github_url_hash, list):
            github_url_hash = bytes(github_url_hash) if github_url_hash else b'\x00' * 32

        # Use explicit None check to avoid treating 0 as falsy
        bounty_amount = raw_data.get('bounty_amount')
        if bounty_amount is None:
            bounty_amount = raw_data.get('bountyAmount', 0)
        target_bounty = raw_data.get('target_bounty')
        if target_bounty is None:
            target_bounty = raw_data.get('targetBounty', 0)

        return ContractIssue(
            id=raw_data.get('id', 0),
            github_url_hash=github_url_hash,
            repository_full_name=raw_data.get('repository_full_name') or raw_data.get('repositoryFullName', ''),
            issue_number=raw_data.get('issue_number') or raw_data.get('issueNumber', 0),
            bounty_amount=int(bounty_amount),
            target_bounty=int(target_bounty),
            status=IssueStatus(raw_data.get('status', 0)),
            registered_at_block=raw_data.get('registered_at_block') or raw_data.get('registeredAtBlock', 0),
            is_fully_funded=int(bounty_amount) >= int(target_bounty),
        )

    def _parse_competition(self, raw_data: dict) -> ContractCompetition:
        """Parse raw contract data into ContractCompetition."""
        # ink! contract uses snake_case keys
        return ContractCompetition(
            id=raw_data.get('id', 0),
            issue_id=raw_data.get('issue_id') or raw_data.get('issueId', 0),
            miner1_hotkey=raw_data.get('miner1_hotkey') or raw_data.get('miner1Hotkey', ''),
            miner2_hotkey=raw_data.get('miner2_hotkey') or raw_data.get('miner2Hotkey', ''),
            start_block=raw_data.get('start_block') or raw_data.get('startBlock', 0),
            submission_window_end_block=raw_data.get('submission_window_end_block') or raw_data.get('submissionWindowEndBlock', 0),
            deadline_block=raw_data.get('deadline_block') or raw_data.get('deadlineBlock', 0),
            status=CompetitionStatus(raw_data.get('status', 0)),
            winner_hotkey=raw_data.get('winner_hotkey') or raw_data.get('winnerHotkey'),
            winning_pr_url_hash=raw_data.get('winning_pr_url_hash') or raw_data.get('winningPrUrlHash'),
            payout_amount=raw_data.get('payout_amount') or raw_data.get('payoutAmount'),
        )

    def _parse_pair_proposal(self, raw_data: dict) -> PairProposal:
        """Parse raw contract data into PairProposal."""
        return PairProposal(
            issue_id=raw_data.get('issue_id') or raw_data.get('issueId', 0),
            miner1_hotkey=raw_data.get('miner1_hotkey') or raw_data.get('miner1Hotkey', ''),
            miner2_hotkey=raw_data.get('miner2_hotkey') or raw_data.get('miner2Hotkey', ''),
            proposer=raw_data.get('proposer', ''),
            proposed_at_block=raw_data.get('proposed_at_block') or raw_data.get('proposedAtBlock', 0),
            total_stake_voted=raw_data.get('total_stake_voted') or raw_data.get('totalStakeVoted', 0),
        )
