#![cfg_attr(not(feature = "std"), no_std, no_main)]

mod errors;
mod events;
mod types;

pub use errors::Error;
pub use types::*;

// ============================================================================
// Chain Extension for Subtensor Staking Operations
// ============================================================================

/// Subtensor chain extension for staking operations.
/// These functions allow the contract to interact with the Subtensor runtime
/// for querying and transferring stake.
///
/// Note: All functions use `handle_status = false` which means they return
/// raw values without automatic error handling from status codes. The caller
/// is responsible for interpreting the return values.
///
/// IMPORTANT: Function 0 returns Option<StakeInfo>, which ink! decodes automatically.
/// The StakeInfo struct in types.rs must match subtensor's StakeInfo exactly.
#[ink::chain_extension(extension = 5001)]
pub trait SubtensorExtension {
    type ErrorCode = ();

    /// Query stake info for hotkey/coldkey/netuid.
    /// Returns Option<StakeInfo> - None if no stake exists, Some(info) with stake details.
    /// ink! handles SCALE decoding automatically.
    #[ink(function = 0, handle_status = false)]
    fn get_stake_info(hotkey: [u8; 32], coldkey: [u8; 32], netuid: u16) -> Option<crate::StakeInfo>;

    /// Transfer stake ownership to a different coldkey.
    /// Amount is in AlphaCurrency (u64), NOT u128!
    /// Returns 0 on success, non-zero error code on failure.
    #[ink(function = 6, handle_status = false)]
    fn transfer_stake(
        destination_coldkey: [u8; 32],
        hotkey: [u8; 32],
        origin_netuid: u16,
        destination_netuid: u16,
        amount: u64,
    ) -> u32;
}

/// Custom environment with Subtensor chain extension.
#[derive(Debug, Clone, PartialEq, Eq)]
#[ink::scale_derive(TypeInfo)]
pub enum CustomEnvironment {}

impl ink::env::Environment for CustomEnvironment {
    const MAX_EVENT_TOPICS: usize = 4;
    type AccountId = ink::primitives::AccountId;
    type Balance = u128;
    type Hash = ink::primitives::Hash;
    type Timestamp = u64;
    type BlockNumber = u32;
    type ChainExtension = SubtensorExtension;
}

#[ink::contract(env = crate::CustomEnvironment)]
mod issue_bounty_manager {
    use crate::events::*;
    use crate::types::*;
    use crate::Error;
    use ink::prelude::string::String;
    use ink::prelude::vec::Vec;
    use ink::storage::Mapping;

    // ========================================================================
    // Constants
    // ========================================================================

    /// Minimum bounty amount: 10 ALPHA (9 decimals)
    pub const MIN_BOUNTY: u128 = 10_000_000_000;

    /// Default submission window: ~2 days at 12s blocks
    pub const DEFAULT_SUBMISSION_WINDOW_BLOCKS: u32 = 14400;

    /// Default competition deadline: ~7 days at 12s blocks
    pub const DEFAULT_COMPETITION_DEADLINE_BLOCKS: u32 = 50400;

    /// Default proposal expiry: ~3.3 hours at 12s blocks
    pub const DEFAULT_PROPOSAL_EXPIRY_BLOCKS: u32 = 1000;

    /// Minimum stake required for consensus: 100 TAO (9 decimals)
    /// This is an absolute threshold - proposals pass when total voted stake
    /// exceeds this amount, rather than requiring a percentage of network stake.
    pub const MIN_CONSENSUS_STAKE: u128 = 100_000_000_000_000;

    // ========================================================================
    // Contract Storage
    // ========================================================================

    #[ink(storage)]
    pub struct IssueBountyManager {
        /// Contract owner with administrative privileges
        owner: AccountId,
        /// Treasury hotkey for staking operations
        treasury_hotkey: AccountId,
        /// Validator hotkey where bounty funds are staked
        validator_hotkey: AccountId,
        /// Subnet ID for this contract
        netuid: u16,
        /// Counter for generating unique issue IDs
        next_issue_id: u64,
        /// Counter for generating unique competition IDs
        next_competition_id: u64,
        /// Unallocated emissions storage (alpha pool)
        alpha_pool: Balance,
        /// Submission window in blocks
        submission_window_blocks: u32,
        /// Competition deadline in blocks
        competition_deadline_blocks: u32,
        /// Proposal expiry in blocks
        proposal_expiry_blocks: u32,

        // Mappings
        /// Mapping from issue ID to Issue struct
        issues: Mapping<u64, Issue>,
        /// Mapping from URL hash to issue ID for deduplication
        url_hash_to_id: Mapping<[u8; 32], u64>,
        /// FIFO queue of issue IDs awaiting bounty fill
        bounty_queue: Vec<u64>,
        /// Mapping from competition ID to Competition struct
        competitions: Mapping<u64, Competition>,
        /// Mapping from issue ID to active competition ID
        issue_to_competition: Mapping<u64, u64>,
        /// Mapping from miner hotkey to active competition ID
        miner_in_competition: Mapping<AccountId, u64>,

        // Pair proposals
        pair_proposals: Mapping<u64, PairProposal>,
        has_pair_proposal: Mapping<u64, bool>,
        pair_proposal_voters: Mapping<(u64, AccountId), bool>,

        // Solution votes
        solution_votes: Mapping<u64, SolutionVote>,
        has_solution_vote: Mapping<u64, bool>,
        solution_vote_voters: Mapping<(u64, AccountId), bool>,

        // Timeout votes
        timeout_votes: Mapping<u64, CancelVote>,
        has_timeout_vote: Mapping<u64, bool>,
        timeout_vote_voters: Mapping<(u64, AccountId), bool>,

        // Cancel votes
        cancel_votes: Mapping<u64, CancelVote>,
        has_cancel_vote: Mapping<u64, bool>,
        cancel_vote_voters: Mapping<(u64, AccountId), bool>,

        // Emission management
        /// Block number of last harvest
        last_harvest_block: u32,
        /// Last known stake for delta calculation (prevents double-counting)
        last_known_stake: Balance,
    }

    impl IssueBountyManager {
        // ========================================================================
        // Constructor
        // ========================================================================

        /// Creates a new IssueBountyManager contract
        #[ink(constructor)]
        pub fn new(
            owner: AccountId,
            treasury_hotkey: AccountId,
            validator_hotkey: AccountId,
            netuid: u16,
        ) -> Self {
            Self {
                owner,
                treasury_hotkey,
                validator_hotkey,
                netuid,
                next_issue_id: 1,
                next_competition_id: 1,
                alpha_pool: 0,
                submission_window_blocks: DEFAULT_SUBMISSION_WINDOW_BLOCKS,
                competition_deadline_blocks: DEFAULT_COMPETITION_DEADLINE_BLOCKS,
                proposal_expiry_blocks: DEFAULT_PROPOSAL_EXPIRY_BLOCKS,
                issues: Mapping::default(),
                url_hash_to_id: Mapping::default(),
                bounty_queue: Vec::new(),
                competitions: Mapping::default(),
                issue_to_competition: Mapping::default(),
                miner_in_competition: Mapping::default(),
                pair_proposals: Mapping::default(),
                has_pair_proposal: Mapping::default(),
                pair_proposal_voters: Mapping::default(),
                solution_votes: Mapping::default(),
                has_solution_vote: Mapping::default(),
                solution_vote_voters: Mapping::default(),
                timeout_votes: Mapping::default(),
                has_timeout_vote: Mapping::default(),
                timeout_vote_voters: Mapping::default(),
                cancel_votes: Mapping::default(),
                has_cancel_vote: Mapping::default(),
                cancel_vote_voters: Mapping::default(),
                last_harvest_block: 0,
                last_known_stake: 0,
            }
        }

        // ========================================================================
        // Issue Registry Functions
        // ========================================================================

        /// Registers a new GitHub issue for competition
        #[ink(message)]
        pub fn register_issue(
            &mut self,
            github_url: String,
            repository_full_name: String,
            issue_number: u32,
            target_bounty: u128,
        ) -> Result<u64, Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }

            if target_bounty < MIN_BOUNTY {
                return Err(Error::BountyTooLow);
            }
            if issue_number == 0 {
                return Err(Error::InvalidIssueNumber);
            }
            if !self.is_valid_repo_name(&repository_full_name) {
                return Err(Error::InvalidRepositoryName);
            }

            let url_hash = self.hash_string(&github_url);

            if self.url_hash_to_id.get(url_hash).is_some() {
                return Err(Error::IssueAlreadyExists);
            }

            let current_block = self.env().block_number();
            let issue_id = self.next_issue_id;
            self.next_issue_id = self.next_issue_id.saturating_add(1);

            let new_issue = Issue {
                id: issue_id,
                github_url_hash: url_hash,
                repository_full_name: repository_full_name.clone(),
                issue_number,
                bounty_amount: 0,
                target_bounty,
                status: IssueStatus::Registered,
                registered_at_block: current_block,
            };

            self.issues.insert(issue_id, &new_issue);
            self.url_hash_to_id.insert(url_hash, &issue_id);
            self.bounty_queue.push(issue_id);

            self.env().emit_event(IssueRegistered {
                issue_id,
                github_url_hash: url_hash,
                repository_full_name,
                issue_number,
                target_bounty,
            });

            // NOTE: No auto-fill - issues stay Registered until harvest or explicit fill
            // This prevents issues from appearing as Active immediately upon registration

            Ok(issue_id)
        }

        /// Cancels an issue before it enters competition
        #[ink(message)]
        pub fn cancel_issue(&mut self, issue_id: u64) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }

            let mut issue = self.issues.get(issue_id).ok_or(Error::IssueNotFound)?;

            if !self.is_modifiable(issue.status) {
                return Err(Error::CannotCancel);
            }

            let returned_bounty = issue.bounty_amount;
            self.alpha_pool = self.alpha_pool.saturating_add(returned_bounty);

            issue.status = IssueStatus::Cancelled;
            issue.bounty_amount = 0;
            self.issues.insert(issue_id, &issue);

            self.remove_from_bounty_queue(issue_id);

            self.env().emit_event(IssueCancelled {
                issue_id,
                returned_bounty,
            });

            Ok(())
        }

        // ========================================================================
        // Bounty Pool Functions
        // ========================================================================

        /// Deposits funds to the alpha pool
        #[ink(message, payable)]
        pub fn deposit_to_pool(&mut self) {
            let amount = self.env().transferred_value();
            if amount == 0 {
                return;
            }
            self.alpha_pool = self.alpha_pool.saturating_add(amount);

            self.env().emit_event(PoolDeposit {
                depositor: self.env().caller(),
                amount,
            });

            self.fill_bounties();
        }

        // ========================================================================
        // Validator Consensus Functions
        // ========================================================================

        /// Proposes a pair of miners for a competition on an issue
        #[ink(message)]
        pub fn propose_pair(
            &mut self,
            issue_id: u64,
            miner1_hotkey: AccountId,
            miner2_hotkey: AccountId,
        ) -> Result<(), Error> {
            if miner1_hotkey == miner2_hotkey {
                return Err(Error::SameMiners);
            }

            let issue = self.issues.get(issue_id).ok_or(Error::IssueNotFound)?;
            if issue.status != IssueStatus::Active {
                return Err(Error::IssueNotActive);
            }

            if self.miner_in_competition.get(miner1_hotkey).is_some() {
                return Err(Error::MinerAlreadyInCompetition);
            }
            if self.miner_in_competition.get(miner2_hotkey).is_some() {
                return Err(Error::MinerAlreadyInCompetition);
            }

            let caller = self.env().caller();
            let stake = self.get_validator_stake(caller);
            if stake == 0 {
                return Err(Error::InsufficientStake);
            }

            let current_block = self.env().block_number();

            let proposal = PairProposal {
                issue_id,
                miner1_hotkey,
                miner2_hotkey,
                proposer: caller,
                proposed_at_block: current_block,
                total_stake_voted: stake,
                votes_count: 1,
            };

            self.pair_proposals.insert(issue_id, &proposal);
            self.has_pair_proposal.insert(issue_id, &true);
            self.pair_proposal_voters.insert((issue_id, caller), &true);

            self.env().emit_event(PairVoteCast {
                issue_id,
                voter: caller,
                stake,
            });

            if self.check_consensus(stake) {
                self.start_competition(issue_id, miner1_hotkey, miner2_hotkey);
                self.clear_pair_proposal(issue_id);
            }

            Ok(())
        }

        /// Votes on an existing pair proposal
        #[ink(message)]
        pub fn vote_pair(&mut self, issue_id: u64) -> Result<(), Error> {
            if !self.has_pair_proposal.get(issue_id).unwrap_or(false) {
                return Err(Error::ProposalNotFound);
            }

            let mut proposal = self
                .pair_proposals
                .get(issue_id)
                .ok_or(Error::ProposalNotFound)?;

            let current_block = self.env().block_number();
            let expiry_block = proposal
                .proposed_at_block
                .saturating_add(self.proposal_expiry_blocks);

            if current_block > expiry_block {
                self.clear_pair_proposal(issue_id);
                return Err(Error::ProposalExpired);
            }

            let caller = self.env().caller();

            if self
                .pair_proposal_voters
                .get((issue_id, caller))
                .unwrap_or(false)
            {
                return Err(Error::AlreadyVoted);
            }

            let issue = self.issues.get(issue_id).ok_or(Error::IssueNotFound)?;
            if issue.status != IssueStatus::Active {
                return Err(Error::IssueNotActive);
            }

            let stake = self.get_validator_stake(caller);
            if stake == 0 {
                return Err(Error::InsufficientStake);
            }

            self.pair_proposal_voters.insert((issue_id, caller), &true);
            proposal.total_stake_voted = proposal.total_stake_voted.saturating_add(stake);
            proposal.votes_count = proposal.votes_count.saturating_add(1);
            self.pair_proposals.insert(issue_id, &proposal);

            self.env().emit_event(PairVoteCast {
                issue_id,
                voter: caller,
                stake,
            });

            if self.check_consensus(proposal.total_stake_voted) {
                self.start_competition(issue_id, proposal.miner1_hotkey, proposal.miner2_hotkey);
                self.clear_pair_proposal(issue_id);
            }

            Ok(())
        }

        /// Votes for a solution winner in an active competition
        #[ink(message)]
        pub fn vote_solution(
            &mut self,
            competition_id: u64,
            winner_hotkey: AccountId,
            pr_url_hash: [u8; 32],
        ) -> Result<(), Error> {
            let competition = self.validate_active_competition(competition_id)?;

            // Solution-specific: validate winner and submission window
            if winner_hotkey != competition.miner1_hotkey
                && winner_hotkey != competition.miner2_hotkey
            {
                return Err(Error::InvalidWinner);
            }
            if self.env().block_number() <= competition.submission_window_end_block {
                return Err(Error::SubmissionWindowNotEnded);
            }

            // Common vote validation
            self.check_not_voted_solution(competition_id, self.env().caller())?;
            let (caller, stake) = self.get_caller_stake_validated()?;

            // Get or create vote, accumulate stake
            let mut vote = self.get_or_create_solution_vote(competition_id, winner_hotkey, pr_url_hash);
            self.solution_vote_voters.insert((competition_id, caller), &true);
            vote.total_stake_voted = vote.total_stake_voted.saturating_add(stake);
            vote.votes_count = vote.votes_count.saturating_add(1);
            self.solution_votes.insert(competition_id, &vote);

            // Check consensus and execute
            if self.check_consensus(vote.total_stake_voted) {
                self.complete_competition(competition_id, winner_hotkey, pr_url_hash);
                self.clear_solution_vote(competition_id);
            }

            Ok(())
        }

        /// Votes to time out a competition that has passed its deadline
        #[ink(message)]
        pub fn vote_timeout(&mut self, competition_id: u64) -> Result<(), Error> {
            let competition = self.validate_active_competition(competition_id)?;

            // Timeout-specific: validate deadline has passed
            if self.env().block_number() <= competition.deadline_block {
                return Err(Error::DeadlineNotPassed);
            }

            // Common vote validation
            self.check_not_voted_timeout(competition_id, self.env().caller())?;
            let (caller, stake) = self.get_caller_stake_validated()?;

            // Get or create vote, accumulate stake
            let mut vote = self.get_or_create_timeout_vote(competition_id);
            self.timeout_vote_voters.insert((competition_id, caller), &true);
            vote.total_stake_voted = vote.total_stake_voted.saturating_add(stake);
            vote.votes_count = vote.votes_count.saturating_add(1);
            self.timeout_votes.insert(competition_id, &vote);

            // Check consensus and execute
            if self.check_consensus(vote.total_stake_voted) {
                self.timeout_competition(competition_id);
                self.clear_timeout_vote(competition_id);
            }

            Ok(())
        }

        /// Votes to cancel a competition (e.g., external solution found)
        #[ink(message)]
        pub fn vote_cancel(
            &mut self,
            competition_id: u64,
            reason_hash: [u8; 32],
        ) -> Result<(), Error> {
            self.validate_active_competition(competition_id)?;

            // Common vote validation
            self.check_not_voted_cancel(competition_id, self.env().caller())?;
            let (caller, stake) = self.get_caller_stake_validated()?;

            // Get or create vote, accumulate stake
            let mut vote = self.get_or_create_cancel_vote(competition_id, reason_hash);
            self.cancel_vote_voters.insert((competition_id, caller), &true);
            vote.total_stake_voted = vote.total_stake_voted.saturating_add(stake);
            vote.votes_count = vote.votes_count.saturating_add(1);
            self.cancel_votes.insert(competition_id, &vote);

            // Check consensus and execute
            if self.check_consensus(vote.total_stake_voted) {
                self.cancel_competition(competition_id, reason_hash);
                self.clear_cancel_vote(competition_id);
            }

            Ok(())
        }

        // ========================================================================
        // Admin Functions
        // ========================================================================

        /// Sets a new owner
        #[ink(message)]
        pub fn set_owner(&mut self, new_owner: AccountId) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }
            self.owner = new_owner;
            Ok(())
        }

        /// Sets a new treasury hotkey
        #[ink(message)]
        pub fn set_treasury_hotkey(&mut self, new_hotkey: AccountId) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }
            self.treasury_hotkey = new_hotkey;
            Ok(())
        }

        /// Sets a new validator hotkey
        #[ink(message)]
        pub fn set_validator_hotkey(&mut self, new_hotkey: AccountId) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }
            self.validator_hotkey = new_hotkey;
            Ok(())
        }

        /// Sets competition timing configuration
        #[ink(message)]
        pub fn set_competition_config(
            &mut self,
            submission_window_blocks: u32,
            competition_deadline_blocks: u32,
            proposal_expiry_blocks: u32,
        ) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }
            self.submission_window_blocks = submission_window_blocks;
            self.competition_deadline_blocks = competition_deadline_blocks;
            self.proposal_expiry_blocks = proposal_expiry_blocks;
            Ok(())
        }

        /// Resets stake tracking to current stake value (OWNER ONLY).
        ///
        /// Emergency function to reset the last_known_stake tracker.
        /// Use this if stake tracking gets out of sync (e.g., after manual
        /// stake operations or contract migration).
        ///
        /// Setting to current stake means next harvest will only count
        /// NEW emissions from this point forward.
        #[ink(message)]
        pub fn reset_stake_tracking(&mut self) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }

            let current_stake = self.get_pending_emissions();
            self.last_known_stake = current_stake;
            Ok(())
        }

        // ========================================================================
        // Emission Harvesting Functions
        // ========================================================================

        /// Query pending emissions (stake on treasury hotkey owned by owner).
        /// Uses chain extension to query Subtensor runtime.
        ///
        /// The chain extension returns Option<StakeInfo>, which ink! decodes automatically.
        #[ink(message)]
        pub fn get_pending_emissions(&self) -> Balance {
            let hotkey_bytes: [u8; 32] = *self.treasury_hotkey.as_ref();
            let coldkey_bytes: [u8; 32] = *self.owner.as_ref();

            // Chain extension returns Option<StakeInfo>, ink! decodes it
            let stake_info = self.env()
                .extension()
                .get_stake_info(hotkey_bytes, coldkey_bytes, self.netuid);

            // Extract stake value from Option<StakeInfo>
            match stake_info {
                Some(info) => info.stake.0 as u128,
                None => 0,
            }
        }

        /// Returns the block number of the last harvest.
        #[ink(message)]
        pub fn get_last_harvest_block(&self) -> u32 {
            self.last_harvest_block
        }

        /// Returns the last known stake used for delta calculation.
        #[ink(message)]
        pub fn get_last_known_stake(&self) -> Balance {
            self.last_known_stake
        }

        /// Harvest emissions and distribute to bounties.
        ///
        /// PERMISSIONLESS - Anyone can call this function.
        ///
        /// Flow:
        /// 1. Query current stake on treasury hotkey (via chain extension)
        /// 2. Calculate delta from last known stake (only count NEW emissions)
        /// 3. Fill pending bounties in queue order
        /// 4. Recycle any remainder to owner's coldkey
        /// 5. If recycling fails, emit HarvestFailed event but keep in alpha_pool
        ///
        /// IMPORTANT: The chain extension returns TOTAL stake, not emissions delta.
        /// We track last_known_stake to compute the actual new emissions.
        #[ink(message)]
        pub fn harvest_emissions(&mut self) -> Result<HarvestResult, Error> {
            // Query current total stake via chain extension
            let current_stake = self.get_pending_emissions();

            // Calculate delta: only new stake since last harvest counts as emissions
            // This prevents double-counting the same stake across multiple harvests
            let pending = current_stake.saturating_sub(self.last_known_stake);

            if pending == 0 {
                // Update tracking even if no new emissions (stake could have been withdrawn)
                self.last_known_stake = current_stake;
                return Ok(HarvestResult {
                    harvested: 0,
                    bounties_filled: 0,
                    recycled: 0,
                });
            }

            // Update last known stake BEFORE distribution to prevent reentrancy issues
            self.last_known_stake = current_stake;

            // Add only the NEW emissions to the alpha pool for bounty filling
            self.alpha_pool = self.alpha_pool.saturating_add(pending);

            let mut bounties_filled: u32 = 0;
            let alpha_before = self.alpha_pool;

            // Fill bounties from alpha pool (existing logic)
            self.fill_bounties();

            // Calculate how much was allocated to bounties
            let bounty_funds_allocated = alpha_before.saturating_sub(self.alpha_pool);

            // Count how many bounties were filled
            if bounty_funds_allocated > 0 {
                // Count filled bounties by checking active issues
                for issue_id in self.bounty_queue.iter() {
                    if let Some(issue) = self.issues.get(*issue_id) {
                        if issue.bounty_amount >= issue.target_bounty {
                            bounties_filled = bounties_filled.saturating_add(1);

                            self.env().emit_event(BountyFilled {
                                issue_id: *issue_id,
                                amount: issue.bounty_amount,
                            });
                        }
                    }
                }
            }

            // Move bounty funds to validator hotkey (stake on Gittensor validator)
            // This uses move_stake which requires Staking proxy
            if bounty_funds_allocated > 0 {
                let amount_u64: u64 = bounty_funds_allocated.try_into().unwrap_or(u64::MAX);

                let move_call = RawCall::proxied_move_stake(
                    &self.owner,              // real: execute as owner (treasury coldkey)
                    &self.treasury_hotkey,    // origin_hotkey: where stake currently is
                    &self.validator_hotkey,   // destination_hotkey: Gittensor validator
                    self.netuid,              // origin_netuid
                    self.netuid,              // destination_netuid (same subnet)
                    amount_u64,
                );

                let move_result = self.env().call_runtime(&move_call);

                if move_result.is_ok() {
                    // CRITICAL: move_stake reduced stake on treasury hotkey, so we must
                    // also reduce last_known_stake to keep the delta calculation accurate.
                    // Otherwise, next harvest would see current_stake < last_known_stake = 0 pending.
                    self.last_known_stake = self.last_known_stake.saturating_sub(bounty_funds_allocated);

                    self.env().emit_event(StakeMovedToValidator {
                        amount: bounty_funds_allocated,
                        validator: self.validator_hotkey,
                    });
                } else {
                    // Log warning but don't fail harvest - stake remains on treasury hotkey
                    self.env().emit_event(StakeMoveFailedWarning {
                        amount: bounty_funds_allocated,
                        validator: self.validator_hotkey,
                    });
                }
            }

            // Recycle any remaining alpha pool (TRUE recycling - destroys tokens)
            let to_recycle = self.alpha_pool;
            let mut recycled: Balance = 0;

            if to_recycle > 0 {
                // Convert u128 to u64 for recycle (AlphaCurrency is u64)
                // Use try_into with fallback to u64::MAX for safety (unlikely to overflow)
                let amount_u64: u64 = to_recycle.try_into().unwrap_or(u64::MAX);

                // Use call_runtime with Proxy::proxy to recycle alpha.
                // The contract acts as a NonCritical proxy for the owner (treasury_coldkey),
                // allowing it to execute recycle_alpha on behalf of the owner.
                // recycle_alpha DESTROYS tokens and reduces SubnetAlphaOut - this is TRUE recycling.
                let proxy_call = RawCall::proxied_recycle_alpha(
                    &self.owner,            // real: execute as owner (treasury_coldkey)
                    &self.treasury_hotkey,  // hotkey to recycle from
                    amount_u64,             // amount to recycle (destroy)
                    self.netuid,            // subnet ID
                );

                let result = self.env().call_runtime(&proxy_call);

                if result.is_ok() {
                    // Recycle successful - tokens destroyed
                    recycled = to_recycle;
                    self.alpha_pool = 0;

                    // CRITICAL: recycle_alpha reduced stake on treasury hotkey, so we must
                    // also reduce last_known_stake to keep the delta calculation accurate.
                    // Otherwise, next harvest would see current_stake < last_known_stake = 0 pending.
                    self.last_known_stake = self.last_known_stake.saturating_sub(recycled);

                    self.env().emit_event(EmissionsRecycled {
                        amount: recycled,
                        destination: self.treasury_hotkey, // Source of recycled tokens (not a transfer destination)
                    });
                } else {
                    // Recycling failed - emit warning event but don't fail harvest
                    // Amount stays in alpha_pool for next harvest attempt
                    // Note: call_runtime doesn't provide detailed error codes like chain extension
                    self.env().emit_event(HarvestFailed {
                        reason: 255, // Generic error code
                        amount: to_recycle,
                    });
                    // Note: alpha_pool keeps the amount, will retry next harvest
                }
            }

            self.last_harvest_block = self.env().block_number();

            self.env().emit_event(EmissionsHarvested {
                amount: pending,
                bounties_filled,
                recycled,
            });

            Ok(HarvestResult {
                harvested: pending,
                bounties_filled,
                recycled,
            })
        }

        /// Pay out a completed bounty to the winning miner.
        ///
        /// Called when a competition is completed and verified.
        /// Transfers stake ownership to the miner's coldkey.
        #[ink(message)]
        pub fn payout_bounty(
            &mut self,
            competition_id: u64,
            miner_coldkey: AccountId,
        ) -> Result<Balance, Error> {
            // Only owner can initiate payouts
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }

            let competition = self
                .competitions
                .get(competition_id)
                .ok_or(Error::CompetitionNotFound)?;

            if competition.status != CompetitionStatus::Completed {
                return Err(Error::BountyNotCompleted);
            }

            let payout_amount = competition.payout_amount;
            if payout_amount == 0 {
                return Err(Error::BountyNotFunded);
            }

            // Convert u128 to u64 for transfer (AlphaCurrency is u64)
            let amount_u64: u64 = payout_amount.try_into().unwrap_or(u64::MAX);

            // Use call_runtime with Proxy::proxy to transfer stake to miner.
            // The contract acts as a Staking proxy for the owner (treasury_coldkey),
            // allowing it to execute transfer_stake on behalf of the owner.
            let proxy_call = RawCall::proxied_transfer_stake(
                &self.owner,           // real: execute as owner
                &miner_coldkey,        // destination_coldkey: pay out to miner
                &self.treasury_hotkey, // hotkey
                self.netuid,           // origin_netuid
                self.netuid,           // destination_netuid
                amount_u64,            // amount
            );

            let result = self.env().call_runtime(&proxy_call);

            if result.is_ok() {
                // Transfer successful
                self.env().emit_event(BountyPaidOut {
                    issue_id: competition.issue_id,
                    miner: miner_coldkey,
                    amount: payout_amount,
                });
                Ok(payout_amount)
            } else {
                Err(Error::TransferFailed)
            }
        }

        // ========================================================================
        // Query Functions (for CLI reads)
        // ========================================================================

        /// Returns the contract owner
        #[ink(message)]
        pub fn owner(&self) -> AccountId {
            self.owner
        }

        /// Returns the treasury hotkey
        #[ink(message)]
        pub fn treasury_hotkey(&self) -> AccountId {
            self.treasury_hotkey
        }

        /// Returns the validator hotkey
        #[ink(message)]
        pub fn validator_hotkey(&self) -> AccountId {
            self.validator_hotkey
        }

        /// Returns the subnet ID
        #[ink(message)]
        pub fn netuid(&self) -> u16 {
            self.netuid
        }

        /// Returns the next issue ID
        #[ink(message)]
        pub fn next_issue_id(&self) -> u64 {
            self.next_issue_id
        }

        /// Returns the next competition ID
        #[ink(message)]
        pub fn next_competition_id(&self) -> u64 {
            self.next_competition_id
        }

        /// Returns the alpha pool balance
        #[ink(message)]
        pub fn get_alpha_pool(&self) -> Balance {
            self.alpha_pool
        }

        /// Returns the submission window blocks
        #[ink(message)]
        pub fn get_submission_window_blocks(&self) -> u32 {
            self.submission_window_blocks
        }

        /// Returns the competition deadline blocks
        #[ink(message)]
        pub fn get_competition_deadline_blocks(&self) -> u32 {
            self.competition_deadline_blocks
        }

        /// Returns the proposal expiry blocks
        #[ink(message)]
        pub fn get_proposal_expiry_blocks(&self) -> u32 {
            self.proposal_expiry_blocks
        }

        /// Returns an issue by ID
        #[ink(message)]
        pub fn get_issue(&self, issue_id: u64) -> Option<Issue> {
            self.issues.get(issue_id)
        }

        /// Returns a competition by ID
        #[ink(message)]
        pub fn get_competition(&self, competition_id: u64) -> Option<Competition> {
            self.competitions.get(competition_id)
        }

        /// Returns a pair proposal for an issue
        #[ink(message)]
        pub fn get_pair_proposal(&self, issue_id: u64) -> Option<PairProposal> {
            if self.has_pair_proposal.get(issue_id).unwrap_or(false) {
                self.pair_proposals.get(issue_id)
            } else {
                None
            }
        }

        /// Returns the competition ID a miner is in (0 if not in any)
        #[ink(message)]
        pub fn get_miner_competition(&self, hotkey: AccountId) -> u64 {
            self.miner_in_competition.get(hotkey).unwrap_or(0)
        }

        /// Returns true if miner is in an active competition
        #[ink(message)]
        pub fn is_miner_in_competition(&self, hotkey: AccountId) -> bool {
            self.miner_in_competition.get(hotkey).is_some()
        }

        /// Returns the issue ID for a URL hash
        #[ink(message)]
        pub fn get_issue_by_url_hash(&self, url_hash: [u8; 32]) -> u64 {
            self.url_hash_to_id.get(url_hash).unwrap_or(0)
        }

        /// Returns the competition ID for an issue
        #[ink(message)]
        pub fn get_issue_competition(&self, issue_id: u64) -> u64 {
            self.issue_to_competition.get(issue_id).unwrap_or(0)
        }

        /// Returns the bounty queue
        #[ink(message)]
        pub fn get_bounty_queue(&self) -> Vec<u64> {
            self.bounty_queue.clone()
        }

        /// Returns all issues with a given status
        #[ink(message)]
        pub fn get_issues_by_status(&self, status: IssueStatus) -> Vec<Issue> {
            let mut result = Vec::new();
            let mut issue_id = 1u64;
            while issue_id < self.next_issue_id {
                if let Some(issue) = self.issues.get(issue_id) {
                    if issue.status == status {
                        result.push(issue);
                    }
                }
                issue_id = issue_id.saturating_add(1);
            }
            result
        }

        /// Returns all active competitions
        #[ink(message)]
        pub fn get_active_competitions(&self) -> Vec<Competition> {
            let mut result = Vec::new();
            let mut comp_id = 1u64;
            while comp_id < self.next_competition_id {
                if let Some(comp) = self.competitions.get(comp_id) {
                    if comp.status == CompetitionStatus::Active {
                        result.push(comp);
                    }
                }
                comp_id = comp_id.saturating_add(1);
            }
            result
        }

        // ========================================================================
        // Internal Functions
        // ========================================================================

        // ========================================================================
        // Vote Processing Helpers
        // ========================================================================

        /// Validates a competition exists and is active.
        fn validate_active_competition(&self, competition_id: u64) -> Result<Competition, Error> {
            let competition = self
                .competitions
                .get(competition_id)
                .ok_or(Error::CompetitionNotFound)?;

            if competition.status != CompetitionStatus::Active {
                return Err(Error::CompetitionNotActive);
            }

            Ok(competition)
        }

        /// Gets the caller's validated stake (returns error if zero).
        fn get_caller_stake_validated(&self) -> Result<(AccountId, u128), Error> {
            let caller = self.env().caller();
            let stake = self.get_validator_stake(caller);
            if stake == 0 {
                return Err(Error::InsufficientStake);
            }
            Ok((caller, stake))
        }

        /// Checks if caller has already voted for a solution.
        fn check_not_voted_solution(&self, competition_id: u64, caller: AccountId) -> Result<(), Error> {
            if self.solution_vote_voters.get((competition_id, caller)).unwrap_or(false) {
                return Err(Error::AlreadyVoted);
            }
            Ok(())
        }

        /// Checks if caller has already voted for timeout.
        fn check_not_voted_timeout(&self, competition_id: u64, caller: AccountId) -> Result<(), Error> {
            if self.timeout_vote_voters.get((competition_id, caller)).unwrap_or(false) {
                return Err(Error::AlreadyVoted);
            }
            Ok(())
        }

        /// Checks if caller has already voted for cancel.
        fn check_not_voted_cancel(&self, competition_id: u64, caller: AccountId) -> Result<(), Error> {
            if self.cancel_vote_voters.get((competition_id, caller)).unwrap_or(false) {
                return Err(Error::AlreadyVoted);
            }
            Ok(())
        }

        /// Gets existing solution vote or creates a new one.
        fn get_or_create_solution_vote(
            &mut self,
            competition_id: u64,
            winner_hotkey: AccountId,
            pr_url_hash: [u8; 32],
        ) -> SolutionVote {
            if self.has_solution_vote.get(competition_id).unwrap_or(false) {
                self.solution_votes.get(competition_id).unwrap_or_default()
            } else {
                self.has_solution_vote.insert(competition_id, &true);
                SolutionVote {
                    competition_id,
                    winner_hotkey,
                    pr_url_hash,
                    total_stake_voted: 0,
                    votes_count: 0,
                }
            }
        }

        /// Gets existing timeout vote or creates a new one.
        fn get_or_create_timeout_vote(&mut self, competition_id: u64) -> CancelVote {
            if self.has_timeout_vote.get(competition_id).unwrap_or(false) {
                self.timeout_votes.get(competition_id).unwrap_or_default()
            } else {
                self.has_timeout_vote.insert(competition_id, &true);
                CancelVote {
                    competition_id,
                    reason_hash: [0u8; 32],
                    total_stake_voted: 0,
                    votes_count: 0,
                }
            }
        }

        /// Gets existing cancel vote or creates a new one.
        fn get_or_create_cancel_vote(&mut self, competition_id: u64, reason_hash: [u8; 32]) -> CancelVote {
            if self.has_cancel_vote.get(competition_id).unwrap_or(false) {
                self.cancel_votes.get(competition_id).unwrap_or_default()
            } else {
                self.has_cancel_vote.insert(competition_id, &true);
                CancelVote {
                    competition_id,
                    reason_hash,
                    total_stake_voted: 0,
                    votes_count: 0,
                }
            }
        }

        // ========================================================================
        // Internal Utility Functions
        // ========================================================================

        /// Validates repository name format (owner/repo)
        fn is_valid_repo_name(&self, name: &str) -> bool {
            let bytes = name.as_bytes();
            if bytes.is_empty() {
                return false;
            }
            let mut slash_pos: Option<usize> = None;

            for (i, &b) in bytes.iter().enumerate() {
                if b == b'/' {
                    if slash_pos.is_some() || i == 0 {
                        return false;
                    }
                    slash_pos = Some(i);
                }
            }

            match slash_pos {
                Some(pos) => {
                    let len = bytes.len();
                    pos < len.saturating_sub(1)
                }
                None => false,
            }
        }

        /// Checks if an issue status allows modification
        fn is_modifiable(&self, status: IssueStatus) -> bool {
            matches!(status, IssueStatus::Registered | IssueStatus::Active)
        }

        /// Hashes a string to [u8; 32] using keccak256
        fn hash_string(&self, s: &str) -> [u8; 32] {
            use ink::env::hash::{HashOutput, Keccak256};
            let mut output = <Keccak256 as HashOutput>::Type::default();
            ink::env::hash_bytes::<Keccak256>(s.as_bytes(), &mut output);
            output
        }

        /// Fills bounties from the alpha pool using FIFO order
        fn fill_bounties(&mut self) {
            let mut i = 0usize;

            while i < self.bounty_queue.len() && self.alpha_pool > 0 {
                let issue_id = self.bounty_queue[i];

                if let Some(mut issue) = self.issues.get(issue_id) {
                    if !self.is_modifiable(issue.status) {
                        self.swap_remove_at(i);
                        continue;
                    }

                    let remaining = issue.target_bounty.saturating_sub(issue.bounty_amount);
                    if remaining == 0 {
                        self.swap_remove_at(i);
                        continue;
                    }

                    let fill_amount = if remaining < self.alpha_pool {
                        remaining
                    } else {
                        self.alpha_pool
                    };

                    issue.bounty_amount = issue.bounty_amount.saturating_add(fill_amount);
                    self.alpha_pool = self.alpha_pool.saturating_sub(fill_amount);

                    let is_fully_funded = issue.bounty_amount >= issue.target_bounty;

                    if is_fully_funded {
                        issue.status = IssueStatus::Active;
                        self.issues.insert(issue_id, &issue);
                        self.swap_remove_at(i);
                    } else {
                        self.issues.insert(issue_id, &issue);
                        i = i.saturating_add(1);
                    }
                } else {
                    self.swap_remove_at(i);
                }
            }
        }

        /// Helper to swap-remove from bounty queue at index
        fn swap_remove_at(&mut self, idx: usize) {
            let len = self.bounty_queue.len();
            if len == 0 {
                return;
            }
            let last_idx = len.saturating_sub(1);
            if idx < last_idx {
                self.bounty_queue.swap(idx, last_idx);
            }
            self.bounty_queue.pop();
        }

        /// Removes an issue from the bounty queue
        fn remove_from_bounty_queue(&mut self, issue_id: u64) {
            if let Some(pos) = self.bounty_queue.iter().position(|&id| id == issue_id) {
                self.swap_remove_at(pos);
            }
        }

        /// Gets a validator's stake via chain extension.
        /// Queries the actual stake the validator has on the treasury hotkey.
        ///
        /// The chain extension returns Option<StakeInfo>, which ink! decodes automatically.
        fn get_validator_stake(&self, validator: AccountId) -> u128 {
            let validator_bytes: [u8; 32] = *validator.as_ref();
            let hotkey_bytes: [u8; 32] = *self.treasury_hotkey.as_ref();

            // Chain extension returns Option<StakeInfo>, ink! decodes it
            let stake_info = self.env()
                .extension()
                .get_stake_info(hotkey_bytes, validator_bytes, self.netuid);

            // Extract stake value from Option<StakeInfo>
            match stake_info {
                Some(info) => info.stake.0 as u128,
                None => 0,
            }
        }

        /// Checks if total voted stake meets minimum consensus threshold.
        /// Uses absolute stake threshold rather than percentage of network stake.
        fn check_consensus(&self, total_voted: u128) -> bool {
            total_voted >= MIN_CONSENSUS_STAKE
        }

        /// Starts a competition from a pair proposal
        fn start_competition(
            &mut self,
            issue_id: u64,
            miner1_hotkey: AccountId,
            miner2_hotkey: AccountId,
        ) -> u64 {
            let current_block = self.env().block_number();
            let competition_id = self.next_competition_id;
            self.next_competition_id = self.next_competition_id.saturating_add(1);

            let competition = Competition {
                id: competition_id,
                issue_id,
                miner1_hotkey,
                miner2_hotkey,
                start_block: current_block,
                submission_window_end_block: current_block
                    .saturating_add(self.submission_window_blocks),
                deadline_block: current_block.saturating_add(self.competition_deadline_blocks),
                status: CompetitionStatus::Active,
                winner_hotkey: AccountId::from([0u8; 32]),
                winning_pr_url_hash: [0u8; 32],
                payout_amount: 0,
            };

            self.competitions.insert(competition_id, &competition);

            self.issue_to_competition.insert(issue_id, &competition_id);
            self.miner_in_competition
                .insert(miner1_hotkey, &competition_id);
            self.miner_in_competition
                .insert(miner2_hotkey, &competition_id);

            if let Some(mut issue) = self.issues.get(issue_id) {
                issue.status = IssueStatus::InCompetition;
                self.issues.insert(issue_id, &issue);
            }

            self.env().emit_event(CompetitionStarted {
                competition_id,
                issue_id,
                miner1_hotkey,
                miner2_hotkey,
                deadline_block: competition.deadline_block,
            });

            competition_id
        }

        /// Completes a competition with a winner
        fn complete_competition(
            &mut self,
            competition_id: u64,
            winner: AccountId,
            pr_hash: [u8; 32],
        ) {
            if let Some(mut competition) = self.competitions.get(competition_id) {
                let issue_id = competition.issue_id;

                if let Some(mut issue) = self.issues.get(issue_id) {
                    let payout = issue.bounty_amount;

                    competition.status = CompetitionStatus::Completed;
                    competition.winner_hotkey = winner;
                    competition.winning_pr_url_hash = pr_hash;
                    competition.payout_amount = payout;
                    self.competitions.insert(competition_id, &competition);

                    issue.status = IssueStatus::Completed;
                    issue.bounty_amount = 0;
                    self.issues.insert(issue_id, &issue);

                    self.miner_in_competition.remove(competition.miner1_hotkey);
                    self.miner_in_competition.remove(competition.miner2_hotkey);
                    self.issue_to_competition.remove(issue_id);

                    self.env().emit_event(CompetitionCompleted {
                        competition_id,
                        issue_id,
                        winner_hotkey: winner,
                        payout,
                        pr_url_hash: pr_hash,
                    });
                }
            }
        }

        /// Times out a competition, returning issue to Active status
        fn timeout_competition(&mut self, competition_id: u64) {
            if let Some(mut competition) = self.competitions.get(competition_id) {
                let issue_id = competition.issue_id;

                competition.status = CompetitionStatus::TimedOut;
                self.competitions.insert(competition_id, &competition);

                if let Some(mut issue) = self.issues.get(issue_id) {
                    issue.status = IssueStatus::Active;
                    self.issues.insert(issue_id, &issue);
                }

                self.miner_in_competition.remove(competition.miner1_hotkey);
                self.miner_in_competition.remove(competition.miner2_hotkey);
                self.issue_to_competition.remove(issue_id);

                self.env().emit_event(CompetitionEnded {
                    competition_id,
                    issue_id,
                    status: 2,
                    reason_hash: [0u8; 32],
                });
            }
        }

        /// Cancels a competition, recycling bounty to pool
        fn cancel_competition(&mut self, competition_id: u64, reason_hash: [u8; 32]) {
            if let Some(mut competition) = self.competitions.get(competition_id) {
                let issue_id = competition.issue_id;

                if let Some(mut issue) = self.issues.get(issue_id) {
                    let recycled_amount = issue.bounty_amount;

                    competition.status = CompetitionStatus::Cancelled;
                    self.competitions.insert(competition_id, &competition);

                    issue.status = IssueStatus::Completed;
                    issue.bounty_amount = 0;
                    self.issues.insert(issue_id, &issue);

                    self.alpha_pool = self.alpha_pool.saturating_add(recycled_amount);

                    self.miner_in_competition.remove(competition.miner1_hotkey);
                    self.miner_in_competition.remove(competition.miner2_hotkey);
                    self.issue_to_competition.remove(issue_id);

                    self.env().emit_event(CompetitionEnded {
                        competition_id,
                        issue_id,
                        status: 3,
                        reason_hash,
                    });
                }
            }
        }

        /// Clears pair proposal data
        fn clear_pair_proposal(&mut self, issue_id: u64) {
            self.pair_proposals.remove(issue_id);
            self.has_pair_proposal.insert(issue_id, &false);
        }

        /// Clears solution vote data
        fn clear_solution_vote(&mut self, competition_id: u64) {
            self.solution_votes.remove(competition_id);
            self.has_solution_vote.insert(competition_id, &false);
        }

        /// Clears timeout vote data
        fn clear_timeout_vote(&mut self, competition_id: u64) {
            self.timeout_votes.remove(competition_id);
            self.has_timeout_vote.insert(competition_id, &false);
        }

        /// Clears cancel vote data
        fn clear_cancel_vote(&mut self, competition_id: u64) {
            self.cancel_votes.remove(competition_id);
            self.has_cancel_vote.insert(competition_id, &false);
        }
    }

    #[cfg(test)]
    mod tests {
        include!("tests.rs");
    }
}
