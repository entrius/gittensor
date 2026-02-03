use ink::prelude::string::String;
use ink::primitives::AccountId;
use scale::{Compact, Decode, Encode};

/// StakeInfo returned by chain extension function 0.
/// Must match subtensor's StakeInfo struct exactly for SCALE decoding.
/// The chain extension returns Option<StakeInfo>, so we decode Some(StakeInfo)
/// by skipping the Option discriminant byte.
#[derive(Debug, Clone, Decode, Encode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub struct StakeInfo {
    pub hotkey: AccountId,
    pub coldkey: AccountId,
    pub netuid: Compact<u16>,
    pub stake: Compact<u64>,      // THE VALUE WE NEED
    pub locked: Compact<u64>,
    pub emission: Compact<u64>,
    pub tao_emission: Compact<u64>,
    pub drain: Compact<u64>,
    pub is_registered: bool,
}

/// Status of an issue in its lifecycle
#[derive(Debug, Clone, Copy, PartialEq, Eq, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub enum IssueStatus {
    /// Issue registered, awaiting bounty fill
    #[default]
    Registered,
    /// Issue has bounty filled, ready for solution
    Active,
    /// Issue has been completed (solution found)
    Completed,
    /// Issue was cancelled
    Cancelled,
}


/// Represents a GitHub issue registered for bounty
#[derive(Debug, Clone, PartialEq, Eq, Encode, Decode, Default)]
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
    /// Solver coldkey (set when issue is completed via consensus)
    pub solver_coldkey: Option<AccountId>,
}


/// Votes for a solution on an issue
#[derive(Debug, Clone, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct SolutionVote {
    /// Issue this vote is for
    pub issue_id: u64,
    /// Proposed solver's hotkey
    pub solver_hotkey: AccountId,
    /// Proposed solver's coldkey (for payout)
    pub solver_coldkey: AccountId,
    /// Hash of the PR URL
    pub pr_url_hash: [u8; 32],
    /// Total stake that has voted
    pub total_stake_voted: u128,
    /// Number of votes cast
    pub votes_count: u32,
}

impl Default for SolutionVote {
    fn default() -> Self {
        Self {
            issue_id: 0,
            solver_hotkey: AccountId::from([0u8; 32]),
            solver_coldkey: AccountId::from([0u8; 32]),
            pr_url_hash: [0u8; 32],
            total_stake_voted: 0,
            votes_count: 0,
        }
    }
}

/// Votes for cancelling an issue
#[derive(Debug, Clone, Encode, Decode, Default)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct CancelVote {
    /// Issue this vote is for
    pub issue_id: u64,
    /// Hash of the reason for cancellation
    pub reason_hash: [u8; 32],
    /// Total stake that has voted
    pub total_stake_voted: u128,
    /// Number of votes cast
    pub votes_count: u32,
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

/// Contract configuration returned by get_config()
#[derive(Debug, Clone, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub struct ContractConfig {
    /// Number of validator votes required for consensus
    pub required_validator_votes: u32,
    /// Subnet ID
    pub netuid: u16,
}
