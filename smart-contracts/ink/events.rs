use ink::prelude::string::String;
use ink::primitives::AccountId;

/// Event emitted when a new issue is registered
#[ink::event]
pub struct IssueRegistered {
    #[ink(topic)]
    pub issue_id: u64,
    pub github_url_hash: [u8; 32],
    pub repository_full_name: String,
    pub issue_number: u32,
    pub target_bounty: u128,
}

/// Event emitted when an issue is cancelled
#[ink::event]
pub struct IssueCancelled {
    #[ink(topic)]
    pub issue_id: u64,
    pub returned_bounty: u128,
}

/// Event emitted when funds are deposited to the bounty pool
#[ink::event]
pub struct PoolDeposit {
    #[ink(topic)]
    pub depositor: AccountId,
    pub amount: u128,
}

// CompetitionStarted - REMOVED in v0 (no competitions)
// CompetitionCompleted - REMOVED in v0 (no competitions)
// CompetitionEnded - REMOVED in v0 (no competitions)
// PairVoteCast - REMOVED in v0 (no competitions)

/// Event emitted when emissions are harvested
#[ink::event]
pub struct EmissionsHarvested {
    #[ink(topic)]
    pub amount: u128,
    pub bounties_filled: u32,
    pub recycled: u128,
}

/// Event emitted when a bounty is filled from emissions
#[ink::event]
pub struct BountyFilled {
    #[ink(topic)]
    pub issue_id: u64,
    pub amount: u128,
}

/// Event emitted when excess emissions are recycled (destroyed via recycle_alpha)
/// True recycling: tokens are destroyed and SubnetAlphaOut is reduced
#[ink::event]
pub struct EmissionsRecycled {
    pub amount: u128,
    /// The hotkey from which tokens were recycled (source, not destination)
    #[ink(topic)]
    pub destination: AccountId,
}

/// Event emitted when a bounty is paid out to a solver
#[ink::event]
pub struct BountyPaidOut {
    #[ink(topic)]
    pub issue_id: u64,
    #[ink(topic)]
    pub miner: AccountId,
    pub amount: u128,
}

/// Event emitted when harvest fails due to recycling error
#[ink::event]
pub struct HarvestFailed {
    /// Error code from transfer_stake chain extension
    #[ink(topic)]
    pub reason: u8,
    /// Amount that failed to recycle
    pub amount: u128,
}

/// Event emitted when stake is moved to the Gittensor validator
#[ink::event]
pub struct StakeMovedToValidator {
    #[ink(topic)]
    pub amount: u128,
    pub validator: AccountId,
}

/// Warning event when stake move to validator fails (non-fatal)
#[ink::event]
pub struct StakeMoveFailedWarning {
    pub amount: u128,
    pub validator: AccountId,
}

/// Event emitted when recycling fails (amount kept in alpha_pool for retry)
#[ink::event]
pub struct RecycleFailed {
    #[ink(topic)]
    pub amount: u128,
}
