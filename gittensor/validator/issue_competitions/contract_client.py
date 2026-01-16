# The MIT License (MIT)
# Copyright 2025 Entrius

"""Client for interacting with the Issues Competition smart contract."""

import hashlib
import json
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

# Default path to contract metadata file
DEFAULT_CONTRACT_METADATA_PATH = Path(__file__).parent.parent.parent.parent.parent / \
    'smart-contracts' / 'solidity' / 'IssueBountyManager.contract'

# Default gas limits for contract calls
DEFAULT_GAS_LIMIT = {
    'ref_time': 10_000_000_000,  # 10 billion
    'proof_size': 500_000,  # 500 KB
}


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
        contract_address: str,
        subtensor: bt.Subtensor,
        metadata_path: Optional[Path] = None,
    ):
        """
        Initialize the contract client.

        Args:
            contract_address: Address of the deployed contract
            subtensor: Bittensor subtensor instance for chain interaction
            metadata_path: Path to the .contract metadata file (optional)
        """
        self.contract_address = contract_address
        self.subtensor = subtensor
        self.metadata_path = metadata_path or DEFAULT_CONTRACT_METADATA_PATH
        self._contract = None
        self._initialized = False

        if not contract_address:
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
            result = self._contract.read(
                self.subtensor.substrate,
                'getAvailableIssues',
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
                'getIssue',
                args={'issueId': issue_id},
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
                'isMinerInCompetition',
                args={'minerHotkey': miner_hotkey},
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
                'getActiveCompetitions',
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
                'getCompetition',
                args={'competitionId': competition_id},
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
                'getPairProposal',
                args={'issueId': issue_id},
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
                'getAlphaPool',
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
                method='proposePair',
                args={
                    'issueId': issue_id,
                    'miner1Hotkey': miner1_hotkey,
                    'miner2Hotkey': miner2_hotkey,
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
                method='votePair',
                args={'issueId': issue_id},
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
                method='voteSolution',
                args={
                    'competitionId': competition_id,
                    'winnerHotkey': winner_hotkey,
                    'prUrlHash': pr_url_hash,
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
                method='voteTimeout',
                args={'competitionId': competition_id},
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
                method='voteCancel',
                args={
                    'competitionId': competition_id,
                    'reasonHash': reason_hash,
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
        # Handle both snake_case and camelCase keys from Solidity contract
        github_url_hash = raw_data.get('github_url_hash') or raw_data.get('githubUrlHash', [])
        if isinstance(github_url_hash, str):
            github_url_hash = bytes.fromhex(github_url_hash.replace('0x', ''))
        elif isinstance(github_url_hash, list):
            github_url_hash = bytes(github_url_hash)

        bounty_amount = raw_data.get('bounty_amount') or raw_data.get('bountyAmount', 0)
        target_bounty = raw_data.get('target_bounty') or raw_data.get('targetBounty', 0)

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
        # Handle both snake_case and camelCase keys from Solidity contract
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
