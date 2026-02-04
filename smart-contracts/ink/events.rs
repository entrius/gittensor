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

/// Event emitted when recycling fails (amount kept in alpha_pool for retry)
#[ink::event]
pub struct RecycleFailed {
    #[ink(topic)]
    pub amount: u128,
}

/// Debug event for stake query debugging
#[ink::event]
pub struct DebugStakeQuery {
    pub treasury_stake: u128,
    pub committed: u128,
    pub available: u128,
}

/// Event emitted when treasury hotkey is changed
#[ink::event]
pub struct TreasuryHotkeyChanged {
    #[ink(topic)]
    pub old_hotkey: AccountId,
    #[ink(topic)]
    pub new_hotkey: AccountId,
    /// Total bounty amount that was reset across all issues
    pub bounties_reset: u128,
    /// Number of issues affected
    pub issues_affected: u32,
}
