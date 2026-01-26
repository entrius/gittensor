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

/// Event emitted when funds are deposited to the alpha pool
#[ink::event]
pub struct PoolDeposit {
    #[ink(topic)]
    pub depositor: AccountId,
    pub amount: u128,
}

/// Event emitted when a competition starts
#[ink::event]
pub struct CompetitionStarted {
    #[ink(topic)]
    pub competition_id: u64,
    #[ink(topic)]
    pub issue_id: u64,
    pub miner1_hotkey: AccountId,
    pub miner2_hotkey: AccountId,
    pub deadline_block: u32,
}

/// Event emitted when a competition is completed with a winner
#[ink::event]
pub struct CompetitionCompleted {
    #[ink(topic)]
    pub competition_id: u64,
    #[ink(topic)]
    pub issue_id: u64,
    #[ink(topic)]
    pub winner_hotkey: AccountId,
    pub payout: u128,
    pub pr_url_hash: [u8; 32],
}

/// Event emitted when a competition ends (timeout or cancelled)
#[ink::event]
pub struct CompetitionEnded {
    #[ink(topic)]
    pub competition_id: u64,
    #[ink(topic)]
    pub issue_id: u64,
    pub status: u8,
    pub reason_hash: [u8; 32],
}

/// Event emitted when a pair vote is cast
#[ink::event]
pub struct PairVoteCast {
    #[ink(topic)]
    pub issue_id: u64,
    #[ink(topic)]
    pub voter: AccountId,
    pub stake: u128,
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

/// Event emitted when excess emissions are recycled to owner
#[ink::event]
pub struct EmissionsRecycled {
    pub amount: u128,
    #[ink(topic)]
    pub destination: AccountId,
}

/// Event emitted when a bounty is paid out to a miner
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
