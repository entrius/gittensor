use ink::prelude::string::String;
use ink::primitives::AccountId;
use scale::{Decode, Encode};

/// Status of an issue in its lifecycle
#[derive(Debug, Clone, Copy, PartialEq, Eq, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub enum IssueStatus {
    /// Issue registered, awaiting bounty fill
    #[default]
    Registered,
    /// Issue has bounty filled, ready for competition
    Active,
    /// Issue is currently in an active competition
    InCompetition,
    /// Issue has been completed (competition resolved)
    Completed,
    /// Issue was cancelled by owner before competition
    Cancelled,
}

/// Status of a competition
#[derive(Debug, Clone, Copy, PartialEq, Eq, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub enum CompetitionStatus {
    /// Competition is active (miners working on solutions)
    #[default]
    Active,
    /// Competition completed with a winner
    Completed,
    /// Competition timed out (no valid solution)
    TimedOut,
    /// Competition cancelled (external solution or invalid)
    Cancelled,
}

/// Represents a GitHub issue registered for competition
#[derive(Debug, Clone, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct Issue {
    /// Unique issue ID
    pub id: u64,
    /// Hash of the GitHub issue URL
    pub github_url_hash: [u8; 32],
    /// Repository in "owner/repo" format
    pub repository_full_name: String,
    /// Issue number within the repository
    pub issue_number: u32,
    /// Current bounty amount allocated
    pub bounty_amount: u128,
    /// Target bounty amount
    pub target_bounty: u128,
    /// Current status of the issue
    pub status: IssueStatus,
    /// Block number when registered
    pub registered_at_block: u32,
}

/// Represents a head-to-head competition between two miners
#[derive(Debug, Clone, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct Competition {
    /// Unique competition ID
    pub id: u64,
    /// ID of the issue being competed for
    pub issue_id: u64,
    /// First miner's hotkey
    pub miner1_hotkey: AccountId,
    /// Second miner's hotkey
    pub miner2_hotkey: AccountId,
    /// Block number when competition started
    pub start_block: u32,
    /// Block when submission window ends
    pub submission_window_end_block: u32,
    /// Block when competition deadline is reached
    pub deadline_block: u32,
    /// Current status of the competition
    pub status: CompetitionStatus,
    /// Winner's hotkey (if completed)
    pub winner_hotkey: AccountId,
    /// Hash of the winning PR URL
    pub winning_pr_url_hash: [u8; 32],
    /// Payout amount to winner
    pub payout_amount: u128,
}

impl Default for Competition {
    fn default() -> Self {
        Self {
            id: 0,
            issue_id: 0,
            miner1_hotkey: AccountId::from([0u8; 32]),
            miner2_hotkey: AccountId::from([0u8; 32]),
            start_block: 0,
            submission_window_end_block: 0,
            deadline_block: 0,
            status: CompetitionStatus::default(),
            winner_hotkey: AccountId::from([0u8; 32]),
            winning_pr_url_hash: [0u8; 32],
            payout_amount: 0,
        }
    }
}

/// A proposal to pair two miners for a competition
#[derive(Debug, Clone, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct PairProposal {
    /// ID of the issue for this proposal
    pub issue_id: u64,
    /// First miner's hotkey
    pub miner1_hotkey: AccountId,
    /// Second miner's hotkey
    pub miner2_hotkey: AccountId,
    /// Proposer's account
    pub proposer: AccountId,
    /// Block when proposal was made
    pub proposed_at_block: u32,
    /// Total stake that has voted for this proposal
    pub total_stake_voted: u128,
    /// Number of votes cast
    pub votes_count: u64,
}

impl Default for PairProposal {
    fn default() -> Self {
        Self {
            issue_id: 0,
            miner1_hotkey: AccountId::from([0u8; 32]),
            miner2_hotkey: AccountId::from([0u8; 32]),
            proposer: AccountId::from([0u8; 32]),
            proposed_at_block: 0,
            total_stake_voted: 0,
            votes_count: 0,
        }
    }
}

/// Votes for a solution winner in a competition
#[derive(Debug, Clone, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct SolutionVote {
    /// Competition this vote is for
    pub competition_id: u64,
    /// Proposed winner's hotkey
    pub winner_hotkey: AccountId,
    /// Hash of the PR URL
    pub pr_url_hash: [u8; 32],
    /// Total stake that has voted
    pub total_stake_voted: u128,
    /// Number of votes cast
    pub votes_count: u64,
}

impl Default for SolutionVote {
    fn default() -> Self {
        Self {
            competition_id: 0,
            winner_hotkey: AccountId::from([0u8; 32]),
            pr_url_hash: [0u8; 32],
            total_stake_voted: 0,
            votes_count: 0,
        }
    }
}

/// Votes for cancelling or timing out a competition
#[derive(Debug, Clone, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct CancelVote {
    /// Competition this vote is for
    pub competition_id: u64,
    /// Hash of the reason for cancellation
    pub reason_hash: [u8; 32],
    /// Total stake that has voted
    pub total_stake_voted: u128,
    /// Number of votes cast
    pub votes_count: u64,
}

/// Result of a harvest_emissions call
#[derive(Debug, Clone, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub struct HarvestResult {
    /// Total amount harvested from emissions
    pub harvested: u128,
    /// Number of bounties filled
    pub bounties_filled: u32,
    /// Amount recycled to owner
    pub recycled: u128,
}
