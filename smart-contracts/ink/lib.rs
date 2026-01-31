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

    /// Number of validator votes required for consensus.
    /// Currently hardcoded to 1 for simplicity.
    ///
    /// TODO: Replace with VotingPower chain extensions once PR #2376 is merged.
    /// Future: check if voter's voting_power / total_voting_power >= threshold
    pub const REQUIRED_VALIDATOR_VOTES: u32 = 1;

    // ========================================================================
    // Contract Storage (v0 - no competitions)
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
        /// Unallocated emissions storage (alpha pool)
        alpha_pool: Balance,

        // Mappings
        /// Mapping from issue ID to Issue struct
        issues: Mapping<u64, Issue>,
        /// Mapping from URL hash to issue ID for deduplication
        url_hash_to_id: Mapping<[u8; 32], u64>,
        /// FIFO queue of issue IDs awaiting bounty fill
        bounty_queue: Vec<u64>,

        // Solution votes (v0 - vote on issues directly, not competitions)
        solution_votes: Mapping<u64, SolutionVote>,
        solution_vote_voters: Mapping<(u64, AccountId), bool>,

        // Issue cancel votes (validators can cancel issues at any stage)
        cancel_issue_votes: Mapping<u64, CancelVote>,
        cancel_issue_voters: Mapping<(u64, AccountId), bool>,

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

        /// Creates a new IssueBountyManager contract (v0 - no competitions)
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
                alpha_pool: 0,
                issues: Mapping::default(),
                url_hash_to_id: Mapping::default(),
                bounty_queue: Vec::new(),
                solution_votes: Mapping::default(),
                solution_vote_voters: Mapping::default(),
                cancel_issue_votes: Mapping::default(),
                cancel_issue_voters: Mapping::default(),
                last_harvest_block: 0,
                last_known_stake: 0,
            }
        }

        // ========================================================================
        // Issue Registry Functions
        // ========================================================================

        /// Registers a new GitHub issue for bounty
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

            Ok(issue_id)
        }

        /// Cancels an issue (owner only)
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
        // Validator Consensus Functions (v0 - simplified)
        // ========================================================================

        /// Votes for a solution on an active issue.
        ///
        /// v0 changes from v1:
        /// - Votes on issue directly (not competition)
        /// - No competition pairing required
        /// - Any bronze+ miner can be voted as solver
        ///
        /// When consensus is reached, the issue is completed and bounty paid out.
        #[ink(message)]
        pub fn vote_solution(
            &mut self,
            issue_id: u64,
            solver_hotkey: AccountId,
            solver_coldkey: AccountId,
            pr_url_hash: [u8; 32],
        ) -> Result<(), Error> {
            let issue = self.issues.get(issue_id).ok_or(Error::IssueNotFound)?;

            if issue.status != IssueStatus::Active {
                return Err(Error::IssueNotActive);
            }

            // Check not already voted
            self.check_not_voted_solution(issue_id, self.env().caller())?;
            let (caller, stake) = self.get_caller_stake_validated()?;

            // Get or create vote, accumulate stake
            let mut vote = self.get_or_create_solution_vote(issue_id, solver_hotkey, pr_url_hash, solver_coldkey);
            self.solution_vote_voters.insert((issue_id, caller), &true);
            vote.total_stake_voted = vote.total_stake_voted.saturating_add(stake);
            vote.votes_count = vote.votes_count.saturating_add(1);
            self.solution_votes.insert(issue_id, &vote);

            // Check consensus and execute (includes auto-payout)
            if self.check_consensus(vote.votes_count) {
                self.complete_issue(issue_id, solver_hotkey, pr_url_hash, solver_coldkey);
                self.clear_solution_vote(issue_id);
            }

            Ok(())
        }

        /// Votes to cancel an issue (e.g., external solution found, issue invalid).
        ///
        /// Works on issues in Registered or Active state.
        #[ink(message)]
        pub fn vote_cancel_issue(
            &mut self,
            issue_id: u64,
            reason_hash: [u8; 32],
        ) -> Result<(), Error> {
            let issue = self.issues.get(issue_id).ok_or(Error::IssueNotFound)?;

            // Can cancel Registered or Active
            if matches!(
                issue.status,
                IssueStatus::Completed | IssueStatus::Cancelled
            ) {
                return Err(Error::IssueAlreadyFinalized);
            }

            // Standard vote validation
            self.check_not_voted_cancel_issue(issue_id, self.env().caller())?;
            let (caller, _stake) = self.get_caller_stake_validated()?;

            // Get or create vote, increment count
            let mut vote = self.get_or_create_cancel_issue_vote(issue_id, reason_hash);
            self.cancel_issue_voters.insert((issue_id, caller), &true);
            vote.votes_count = vote.votes_count.saturating_add(1);
            self.cancel_issue_votes.insert(issue_id, &vote);

            // Check consensus and execute
            if self.check_consensus(vote.votes_count) {
                self.execute_cancel_issue(issue_id, reason_hash);
                self.clear_cancel_issue_vote(issue_id);
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

        // ========================================================================
        // Emission Harvesting Functions
        // ========================================================================

        /// Query total stake on treasury hotkey owned by owner.
        /// Uses chain extension to query Subtensor runtime.
        #[ink(message)]
        pub fn get_treasury_stake(&self) -> Balance {
            let hotkey_bytes: [u8; 32] = *self.treasury_hotkey.as_ref();
            let coldkey_bytes: [u8; 32] = *self.owner.as_ref();

            let stake_info = self.env()
                .extension()
                .get_stake_info(hotkey_bytes, coldkey_bytes, self.netuid);

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
        #[ink(message)]
        pub fn harvest_emissions(&mut self) -> Result<HarvestResult, Error> {
            // Query current total stake via chain extension
            let current_stake = self.get_treasury_stake();

            // Calculate delta: only new stake since last harvest counts as emissions
            let pending = current_stake.saturating_sub(self.last_known_stake);

            if pending == 0 {
                self.last_known_stake = current_stake;
                return Ok(HarvestResult {
                    harvested: 0,
                    bounties_filled: 0,
                    recycled: 0,
                });
            }

            // Update last known stake BEFORE distribution
            self.last_known_stake = current_stake;

            // Add only the NEW emissions to the alpha pool for bounty filling
            self.alpha_pool = self.alpha_pool.saturating_add(pending);

            let mut bounties_filled: u32 = 0;
            let alpha_before = self.alpha_pool;

            // Fill bounties from alpha pool
            self.fill_bounties();

            // Calculate how much was allocated to bounties
            let bounty_funds_allocated = alpha_before.saturating_sub(self.alpha_pool);

            // Count how many bounties were filled
            if bounty_funds_allocated > 0 {
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

            // Move bounty funds to validator hotkey
            if bounty_funds_allocated > 0 {
                let amount_u64: u64 = bounty_funds_allocated.try_into().unwrap_or(u64::MAX);

                let move_call = RawCall::proxied_move_stake(
                    &self.owner,
                    &self.treasury_hotkey,
                    &self.validator_hotkey,
                    self.netuid,
                    self.netuid,
                    amount_u64,
                );

                let move_result = self.env().call_runtime(&move_call);

                if move_result.is_ok() {
                    self.last_known_stake = self.last_known_stake.saturating_sub(bounty_funds_allocated);

                    self.env().emit_event(StakeMovedToValidator {
                        amount: bounty_funds_allocated,
                        validator: self.validator_hotkey,
                    });
                } else {
                    self.env().emit_event(StakeMoveFailedWarning {
                        amount: bounty_funds_allocated,
                        validator: self.validator_hotkey,
                    });
                }
            }

            // Recycle any remaining alpha pool
            let to_recycle = self.alpha_pool;
            let mut recycled: Balance = 0;

            if to_recycle > 0 {
                let amount_u64: u64 = to_recycle.try_into().unwrap_or(u64::MAX);

                let proxy_call = RawCall::proxied_recycle_alpha(
                    &self.owner,
                    &self.treasury_hotkey,
                    amount_u64,
                    self.netuid,
                );

                let result = self.env().call_runtime(&proxy_call);

                if result.is_ok() {
                    recycled = to_recycle;
                    self.alpha_pool = 0;
                    self.last_known_stake = self.last_known_stake.saturating_sub(recycled);

                    self.env().emit_event(EmissionsRecycled {
                        amount: recycled,
                        destination: self.treasury_hotkey,
                    });
                } else {
                    self.env().emit_event(HarvestFailed {
                        reason: 255,
                        amount: to_recycle,
                    });
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

        /// Manual payout fallback for edge cases where auto-payout failed.
        #[ink(message)]
        pub fn payout_bounty(
            &mut self,
            issue_id: u64,
            solver_coldkey: AccountId,
        ) -> Result<Balance, Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }

            self.execute_payout(issue_id, solver_coldkey)
        }

        // ========================================================================
        // Query Functions
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

        /// Returns the alpha pool balance
        #[ink(message)]
        pub fn get_alpha_pool(&self) -> Balance {
            self.alpha_pool
        }

        /// Returns an issue by ID
        #[ink(message)]
        pub fn get_issue(&self, issue_id: u64) -> Option<Issue> {
            self.issues.get(issue_id)
        }

        /// Returns the issue ID for a URL hash
        #[ink(message)]
        pub fn get_issue_by_url_hash(&self, url_hash: [u8; 32]) -> u64 {
            self.url_hash_to_id.get(url_hash).unwrap_or(0)
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

        /// Returns all contract configuration in a single call.
        #[ink(message)]
        pub fn get_config(&self) -> ContractConfig {
            ContractConfig {
                required_validator_votes: REQUIRED_VALIDATOR_VOTES,
                netuid: self.netuid,
            }
        }

        // ========================================================================
        // Internal Functions
        // ========================================================================

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
        fn check_not_voted_solution(&self, issue_id: u64, caller: AccountId) -> Result<(), Error> {
            if self.solution_vote_voters.get((issue_id, caller)).unwrap_or(false) {
                return Err(Error::AlreadyVoted);
            }
            Ok(())
        }

        /// Checks if caller has already voted to cancel an issue.
        fn check_not_voted_cancel_issue(&self, issue_id: u64, caller: AccountId) -> Result<(), Error> {
            if self.cancel_issue_voters.get((issue_id, caller)).unwrap_or(false) {
                return Err(Error::AlreadyVoted);
            }
            Ok(())
        }

        /// Gets existing solution vote or creates a new one.
        fn get_or_create_solution_vote(
            &mut self,
            issue_id: u64,
            solver_hotkey: AccountId,
            pr_url_hash: [u8; 32],
            solver_coldkey: AccountId,
        ) -> SolutionVote {
            if let Some(vote) = self.solution_votes.get(issue_id) {
                vote
            } else {
                SolutionVote {
                    issue_id,
                    solver_hotkey,
                    solver_coldkey,
                    pr_url_hash,
                    total_stake_voted: 0,
                    votes_count: 0,
                }
            }
        }

        /// Gets existing issue cancel vote or creates a new one.
        fn get_or_create_cancel_issue_vote(&mut self, issue_id: u64, reason_hash: [u8; 32]) -> CancelVote {
            if let Some(vote) = self.cancel_issue_votes.get(issue_id) {
                vote
            } else {
                CancelVote {
                    issue_id,
                    reason_hash,
                    total_stake_voted: 0,
                    votes_count: 0,
                }
            }
        }

        /// Clears issue cancel vote data
        fn clear_cancel_issue_vote(&mut self, issue_id: u64) {
            self.cancel_issue_votes.remove(issue_id);
        }

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
        fn get_validator_stake(&self, validator: AccountId) -> u128 {
            let validator_bytes: [u8; 32] = *validator.as_ref();
            let hotkey_bytes: [u8; 32] = *self.treasury_hotkey.as_ref();

            let stake_info = self.env()
                .extension()
                .get_stake_info(hotkey_bytes, validator_bytes, self.netuid);

            match stake_info {
                Some(info) => info.stake.0 as u128,
                None => 0,
            }
        }

        /// Checks if vote count meets minimum consensus threshold.
        fn check_consensus(&self, votes_count: u32) -> bool {
            votes_count >= REQUIRED_VALIDATOR_VOTES
        }

        /// Completes an issue with a solution and triggers auto-payout (v0 - no competitions)
        fn complete_issue(
            &mut self,
            issue_id: u64,
            solver_hotkey: AccountId,
            pr_hash: [u8; 32],
            solver_coldkey: AccountId,
        ) {
            if let Some(mut issue) = self.issues.get(issue_id) {
                let payout = issue.bounty_amount;

                issue.status = IssueStatus::Completed;
                issue.bounty_amount = 0;
                self.issues.insert(issue_id, &issue);

                self.env().emit_event(BountyPaidOut {
                    issue_id,
                    miner: solver_coldkey,
                    amount: payout,
                });

                // Auto-payout to solver
                if payout > 0 {
                    let _ = self.execute_payout_internal(issue_id, solver_coldkey, payout);
                }

                // Store solution info for reference (optional - can be queried from events)
                let _ = solver_hotkey;
                let _ = pr_hash;
            }
        }

        /// Executes issue cancellation
        fn execute_cancel_issue(&mut self, issue_id: u64, _reason_hash: [u8; 32]) {
            let mut issue = match self.issues.get(issue_id) {
                Some(i) => i,
                None => return,
            };

            let returned_bounty = issue.bounty_amount;

            self.remove_from_bounty_queue(issue_id);
            let _ = self.recycle(returned_bounty);

            issue.status = IssueStatus::Cancelled;
            issue.bounty_amount = 0;
            self.issues.insert(issue_id, &issue);

            self.env().emit_event(IssueCancelled {
                issue_id,
                returned_bounty,
            });
        }

        /// Internal payout execution - transfers bounty to solver's coldkey
        fn execute_payout(
            &mut self,
            issue_id: u64,
            solver_coldkey: AccountId,
        ) -> Result<Balance, Error> {
            let issue = self
                .issues
                .get(issue_id)
                .ok_or(Error::IssueNotFound)?;

            if issue.status != IssueStatus::Completed {
                return Err(Error::BountyNotCompleted);
            }

            // Get payout amount from solution vote (stored before completion)
            let vote = self.solution_votes.get(issue_id).ok_or(Error::BountyNotFunded)?;
            let payout_amount = vote.total_stake_voted; // Note: this should be bounty amount, not stake

            if payout_amount == 0 {
                return Err(Error::BountyNotFunded);
            }

            self.execute_payout_internal(issue_id, solver_coldkey, payout_amount)
        }

        /// Internal payout helper
        fn execute_payout_internal(
            &mut self,
            issue_id: u64,
            solver_coldkey: AccountId,
            payout_amount: Balance,
        ) -> Result<Balance, Error> {
            let amount_u64: u64 = payout_amount.try_into().unwrap_or(u64::MAX);

            let proxy_call = RawCall::proxied_transfer_stake(
                &self.owner,
                &solver_coldkey,
                &self.validator_hotkey,
                self.netuid,
                self.netuid,
                amount_u64,
            );

            let result = self.env().call_runtime(&proxy_call);

            if result.is_ok() {
                self.env().emit_event(BountyPaidOut {
                    issue_id,
                    miner: solver_coldkey,
                    amount: payout_amount,
                });
                Ok(payout_amount)
            } else {
                Err(Error::TransferFailed)
            }
        }

        /// Recycles (destroys) alpha tokens via runtime call.
        fn recycle(&mut self, amount: Balance) -> bool {
            if amount == 0 {
                return true;
            }

            let amount_u64: u64 = amount.try_into().unwrap_or(u64::MAX);

            let proxy_call = RawCall::proxied_recycle_alpha(
                &self.owner,
                &self.treasury_hotkey,
                amount_u64,
                self.netuid,
            );

            let result = self.env().call_runtime(&proxy_call);

            if result.is_ok() {
                self.last_known_stake = self.last_known_stake.saturating_sub(amount);
                self.env().emit_event(EmissionsRecycled {
                    amount,
                    destination: self.treasury_hotkey,
                });
                true
            } else {
                self.alpha_pool = self.alpha_pool.saturating_add(amount);
                self.env().emit_event(RecycleFailed { amount });
                false
            }
        }

        /// Clears solution vote data
        fn clear_solution_vote(&mut self, issue_id: u64) {
            self.solution_votes.remove(issue_id);
        }
    }
}
