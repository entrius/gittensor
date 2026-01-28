use ink::prelude::string::String;
use ink::prelude::vec::Vec;
use ink::primitives::AccountId;
use scale::{Compact, Decode, Encode, Output};

// =============================================================================
// Pallet Indices (from construct_runtime!)
// =============================================================================

/// SubtensorModule pallet index in the runtime
pub const SUBTENSOR_MODULE_PALLET_INDEX: u8 = 7;

/// Proxy pallet index in the runtime
pub const PROXY_PALLET_INDEX: u8 = 16;

/// transfer_stake call variant index within SubtensorModule
/// NOTE: This MUST match the order in the pallet's Call enum.
/// Verify with: subtensor/pallets/subtensor/src/macros/dispatches.rs
pub const TRANSFER_STAKE_CALL_INDEX: u8 = 86;

/// move_stake call variant index within SubtensorModule
/// Verified: subtensor/pallets/subtensor/src/macros/dispatches.rs:1618
pub const MOVE_STAKE_CALL_INDEX: u8 = 85;

/// recycle_alpha call variant index within SubtensorModule
/// Verified: subtensor/pallets/subtensor/src/macros/dispatches.rs:1998
/// Recycles alpha tokens, destroying them and reducing SubnetAlphaOut
pub const RECYCLE_ALPHA_CALL_INDEX: u8 = 101;

/// ProxyType::Staking variant index (for move_stake)
/// From Subtensor runtime (verified via substrate encoding):
/// Any=0, Owner=1, NonCritical=2, Governance=7, Staking=8, Transfer=10
pub const PROXY_TYPE_STAKING: u8 = 8;

/// ProxyType::Transfer variant index (for transfer_stake)
/// transfer_stake requires Transfer proxy type, NOT Staking
pub const PROXY_TYPE_TRANSFER: u8 = 10;

/// ProxyType::NonCritical variant index (for recycle_alpha)
/// recycle_alpha is NOT in Staking or Transfer filters, but IS allowed by NonCritical
/// NonCritical allows all calls EXCEPT: dissolve_network, root_register, burned_register, Sudo
pub const PROXY_TYPE_NON_CRITICAL: u8 = 2;

// =============================================================================
// Raw Call Wrapper for call_runtime
// =============================================================================

/// Wrapper for pre-encoded runtime call bytes.
/// When encoded, outputs the raw bytes without any wrapping (no length prefix).
/// Used with `env().call_runtime()` to dispatch pre-encoded calls.
#[derive(Debug, Clone)]
pub struct RawCall(pub Vec<u8>);

impl Encode for RawCall {
    fn encode(&self) -> Vec<u8> {
        self.0.clone()
    }

    fn encode_to<T: Output + ?Sized>(&self, dest: &mut T) {
        dest.write(&self.0);
    }

    fn size_hint(&self) -> usize {
        self.0.len()
    }
}

impl RawCall {
    /// Encode a proxied transfer_stake call.
    ///
    /// Creates a Proxy::proxy call wrapping a SubtensorModule::transfer_stake call.
    /// The proxy pallet will validate that the caller (contract) is a Transfer proxy
    /// for the `real` account before executing the inner call with `real` as origin.
    ///
    /// # Arguments
    /// * `real` - The account to execute as (owner/treasury coldkey)
    /// * `destination_coldkey` - Where to transfer stake ownership to
    /// * `hotkey` - The hotkey the stake is on
    /// * `origin_netuid` - Source subnet ID
    /// * `destination_netuid` - Target subnet ID
    /// * `amount` - Amount of alpha to transfer (u64)
    pub fn proxied_transfer_stake(
        real: &AccountId,
        destination_coldkey: &AccountId,
        hotkey: &AccountId,
        origin_netuid: u16,
        destination_netuid: u16,
        amount: u64,
    ) -> Self {
        let mut call_bytes = Vec::with_capacity(128);

        // Proxy pallet index
        call_bytes.push(PROXY_PALLET_INDEX);

        // proxy() is the first call variant (index 0)
        call_bytes.push(0);

        // real: MultiAddress<AccountId, ()>
        // MultiAddress::Id variant = 0, then 32 bytes of AccountId
        call_bytes.push(0);
        call_bytes.extend_from_slice(real.as_ref());

        // force_proxy_type: Option<ProxyType>
        // Some = 1, then ProxyType::Transfer (transfer_stake requires Transfer proxy)
        call_bytes.push(1);
        call_bytes.push(PROXY_TYPE_TRANSFER);

        // call: Box<RuntimeCall> - the inner transfer_stake call
        // SubtensorModule pallet index
        call_bytes.push(SUBTENSOR_MODULE_PALLET_INDEX);

        // transfer_stake call variant index
        call_bytes.push(TRANSFER_STAKE_CALL_INDEX);

        // transfer_stake arguments:
        // destination_coldkey: AccountId (32 bytes)
        call_bytes.extend_from_slice(destination_coldkey.as_ref());

        // hotkey: AccountId (32 bytes)
        call_bytes.extend_from_slice(hotkey.as_ref());

        // origin_netuid: u16 (2 bytes, little-endian)
        call_bytes.extend_from_slice(&origin_netuid.to_le_bytes());

        // destination_netuid: u16 (2 bytes, little-endian)
        call_bytes.extend_from_slice(&destination_netuid.to_le_bytes());

        // alpha_amount: u64 (8 bytes, little-endian)
        call_bytes.extend_from_slice(&amount.to_le_bytes());

        Self(call_bytes)
    }

    /// Encode a proxied move_stake call.
    ///
    /// Creates a Proxy::proxy call wrapping a SubtensorModule::move_stake call.
    /// The proxy pallet will validate that the caller (contract) is a Staking proxy
    /// for the `real` account before executing the inner call with `real` as origin.
    ///
    /// move_stake moves stake from one hotkey to another within the same coldkey.
    /// Used to stake bounty funds on the Gittensor validator.
    ///
    /// # Arguments
    /// * `real` - The account to execute as (owner/treasury coldkey)
    /// * `origin_hotkey` - Source hotkey (treasury_hotkey)
    /// * `destination_hotkey` - Target hotkey (validator_hotkey)
    /// * `origin_netuid` - Source subnet ID
    /// * `destination_netuid` - Target subnet ID
    /// * `amount` - Amount of alpha to move (u64)
    pub fn proxied_move_stake(
        real: &AccountId,
        origin_hotkey: &AccountId,
        destination_hotkey: &AccountId,
        origin_netuid: u16,
        destination_netuid: u16,
        amount: u64,
    ) -> Self {
        let mut call_bytes = Vec::with_capacity(128);

        // Proxy pallet index
        call_bytes.push(PROXY_PALLET_INDEX);

        // proxy() is the first call variant (index 0)
        call_bytes.push(0);

        // real: MultiAddress<AccountId, ()>
        // MultiAddress::Id variant = 0, then 32 bytes of AccountId
        call_bytes.push(0);
        call_bytes.extend_from_slice(real.as_ref());

        // force_proxy_type: Option<ProxyType>
        // Some = 1, then ProxyType::Staking (move_stake requires Staking proxy)
        call_bytes.push(1);
        call_bytes.push(PROXY_TYPE_STAKING);

        // call: Box<RuntimeCall> - the inner move_stake call
        // SubtensorModule pallet index
        call_bytes.push(SUBTENSOR_MODULE_PALLET_INDEX);

        // move_stake call variant index
        call_bytes.push(MOVE_STAKE_CALL_INDEX);

        // move_stake arguments:
        // origin_hotkey: AccountId (32 bytes)
        call_bytes.extend_from_slice(origin_hotkey.as_ref());

        // destination_hotkey: AccountId (32 bytes)
        call_bytes.extend_from_slice(destination_hotkey.as_ref());

        // origin_netuid: u16 (2 bytes, little-endian)
        call_bytes.extend_from_slice(&origin_netuid.to_le_bytes());

        // destination_netuid: u16 (2 bytes, little-endian)
        call_bytes.extend_from_slice(&destination_netuid.to_le_bytes());

        // alpha_amount: u64 (8 bytes, little-endian)
        call_bytes.extend_from_slice(&amount.to_le_bytes());

        Self(call_bytes)
    }

    /// Encode a proxied recycle_alpha call.
    ///
    /// Creates a Proxy::proxy call wrapping a SubtensorModule::recycle_alpha call.
    /// The proxy pallet will validate that the caller (contract) is a NonCritical proxy
    /// for the `real` account before executing the inner call with `real` as origin.
    ///
    /// recycle_alpha DESTROYS alpha tokens and reduces SubnetAlphaOut.
    /// This is TRUE recycling - tokens cease to exist.
    ///
    /// NOTE: recycle_alpha is NOT in Staking or Transfer proxy filters.
    /// It requires NonCritical (or Any) proxy type.
    ///
    /// # Arguments
    /// * `real` - The account to execute as (owner/treasury coldkey)
    /// * `hotkey` - The hotkey to recycle alpha from
    /// * `amount` - Amount of alpha to recycle (u64)
    /// * `netuid` - Subnet ID
    pub fn proxied_recycle_alpha(
        real: &AccountId,
        hotkey: &AccountId,
        amount: u64,
        netuid: u16,
    ) -> Self {
        let mut call_bytes = Vec::with_capacity(128);

        // Proxy pallet index
        call_bytes.push(PROXY_PALLET_INDEX);

        // proxy() is the first call variant (index 0)
        call_bytes.push(0);

        // real: MultiAddress<AccountId, ()>
        // MultiAddress::Id variant = 0, then 32 bytes of AccountId
        call_bytes.push(0);
        call_bytes.extend_from_slice(real.as_ref());

        // force_proxy_type: Option<ProxyType>
        // Some = 1, then ProxyType::NonCritical (recycle_alpha requires NonCritical)
        call_bytes.push(1);
        call_bytes.push(PROXY_TYPE_NON_CRITICAL);

        // call: Box<RuntimeCall> - the inner recycle_alpha call
        // SubtensorModule pallet index
        call_bytes.push(SUBTENSOR_MODULE_PALLET_INDEX);

        // recycle_alpha call variant index
        call_bytes.push(RECYCLE_ALPHA_CALL_INDEX);

        // recycle_alpha arguments:
        // hotkey: AccountId (32 bytes)
        call_bytes.extend_from_slice(hotkey.as_ref());

        // amount: u64 (8 bytes, little-endian)
        call_bytes.extend_from_slice(&amount.to_le_bytes());

        // netuid: u16 (2 bytes, little-endian)
        call_bytes.extend_from_slice(&netuid.to_le_bytes());

        Self(call_bytes)
    }
}

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
#[derive(Debug, Clone, PartialEq, Eq, Encode, Decode)]
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
