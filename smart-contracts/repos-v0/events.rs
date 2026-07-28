use ink::prelude::string::String;
use ink::prelude::vec::Vec;
use ink::primitives::{AccountId, Hash};

/// A repository was registered (fee paid and recycled).
/// Consumed by the das-github-mirror reconciler: track + deep-backfill.
#[ink::event]
pub struct RepositoryRegistered {
    #[ink(topic)]
    pub github_id: u64,
    pub full_name: String,
    pub owner: AccountId,
    pub fee_paid: u64,
    pub reg_block: u64,
}

/// A repository was removed by its owner (forced = team escape hatch).
/// Consumed by the das-github-mirror reconciler: untrack.
#[ink::event]
pub struct RepositoryDeregistered {
    #[ink(topic)]
    pub github_id: u64,
    pub full_name: String,
    pub forced: bool,
}

/// A repository was pruned to free a slot for `replaced_by`.
/// Consumed by the das-github-mirror reconciler: untrack.
#[ink::event]
pub struct RepositoryPruned {
    #[ink(topic)]
    pub github_id: u64,
    pub full_name: String,
    pub replaced_by: u64,
}

/// Repository renamed on GitHub; id is stable, metadata updated.
#[ink::event]
pub struct FullNameUpdated {
    #[ink(topic)]
    pub github_id: u64,
    pub old_full_name: String,
    pub new_full_name: String,
}

#[ink::event]
pub struct RepoOwnershipTransferred {
    #[ink(topic)]
    pub github_id: u64,
    pub old_owner: AccountId,
    pub new_owner: AccountId,
}

#[ink::event]
pub struct ParamSet {
    #[ink(topic)]
    pub github_id: u64,
    pub key: u8,
    pub value: u64,
}

#[ink::event]
pub struct LabelMultiplierSet {
    #[ink(topic)]
    pub github_id: u64,
    pub label: String,
    pub value: u64,
}

#[ink::event]
pub struct LabelMultiplierRemoved {
    #[ink(topic)]
    pub github_id: u64,
    pub label: String,
}

#[ink::event]
pub struct BranchPatternsSet {
    #[ink(topic)]
    pub github_id: u64,
    pub patterns: Vec<String>,
}

#[ink::event]
pub struct BasketSet {
    #[ink(topic)]
    pub hotkey: AccountId,
    pub entries: Vec<(u64, u16)>,
}

#[ink::event]
pub struct BasketCleared {
    #[ink(topic)]
    pub hotkey: AccountId,
}

#[ink::event]
pub struct VoterAdded {
    #[ink(topic)]
    pub hotkey: AccountId,
}

#[ink::event]
pub struct VoterRemoved {
    #[ink(topic)]
    pub hotkey: AccountId,
}

#[ink::event]
pub struct BoundsSet {
    #[ink(topic)]
    pub key: u8,
    pub min: u64,
    pub max: u64,
}

#[ink::event]
pub struct ConfigUpdated {}

#[ink::event]
pub struct PausedSet {
    pub paused: bool,
}

#[ink::event]
pub struct OwnerChanged {
    #[ink(topic)]
    pub old_owner: AccountId,
    #[ink(topic)]
    pub new_owner: AccountId,
}

#[ink::event]
pub struct CodeHashSet {
    #[ink(topic)]
    pub code_hash: Hash,
}
