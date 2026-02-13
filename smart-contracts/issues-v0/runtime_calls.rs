use ink::prelude::vec::Vec;
use ink::primitives::AccountId;

// =============================================================================
// Pallet Indices (from construct_runtime!)
// =============================================================================

/// SubtensorModule pallet index in the runtime
pub const SUBTENSOR_MODULE_PALLET_INDEX: u8 = 7;

/// transfer_stake call variant index within SubtensorModule
/// NOTE: This MUST match the order in the pallet's Call enum.
/// Verify with: subtensor/pallets/subtensor/src/macros/dispatches.rs
pub const TRANSFER_STAKE_CALL_INDEX: u8 = 86;

/// recycle_alpha call variant index within SubtensorModule
/// Verified: subtensor/pallets/subtensor/src/macros/dispatches.rs:1998
/// Recycles alpha tokens, destroying them and reducing SubnetAlphaOut
pub const RECYCLE_ALPHA_CALL_INDEX: u8 = 101;

/// ProxyType::Transfer variant index (for transfer_stake)
/// From Subtensor runtime (verified via substrate encoding):
/// Any=0, Owner=1, NonCritical=2, Governance=7, Staking=8, Transfer=10
pub const PROXY_TYPE_TRANSFER: u8 = 10;

/// ProxyType::NonCritical variant index (for recycle_alpha)
/// recycle_alpha is NOT in Staking or Transfer filters, but IS allowed by NonCritical
/// NonCritical allows all calls EXCEPT: dissolve_network, root_register, burned_register, Sudo
pub const PROXY_TYPE_NON_CRITICAL: u8 = 2;

// =============================================================================
// Inner Call Encoder for ProxyCall Chain Extension (func 16)
// =============================================================================

/// Encodes bare RuntimeCall bytes (without proxy wrapper).
/// Used with the ProxyCall chain extension (function 16), which handles
/// proxy validation internally. Only the inner call needs to be encoded.
pub struct InnerCall;

impl InnerCall {
    /// Encode a bare transfer_stake RuntimeCall.
    ///
    /// The ProxyCall chain extension wraps this in Proxy::proxy automatically.
    /// Only the inner SubtensorModule::transfer_stake call is encoded here.
    ///
    /// # Arguments
    /// * `destination_coldkey` - Where to transfer stake ownership to
    /// * `hotkey` - The hotkey the stake is on
    /// * `origin_netuid` - Source subnet ID
    /// * `destination_netuid` - Target subnet ID
    /// * `amount` - Amount of alpha to transfer (u64)
    pub fn transfer_stake(
        destination_coldkey: &AccountId,
        hotkey: &AccountId,
        origin_netuid: u16,
        destination_netuid: u16,
        amount: u64,
    ) -> Vec<u8> {
        let mut call_bytes = Vec::with_capacity(78);

        // SubtensorModule pallet index
        call_bytes.push(SUBTENSOR_MODULE_PALLET_INDEX);

        // transfer_stake call variant index
        call_bytes.push(TRANSFER_STAKE_CALL_INDEX);

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

        call_bytes
    }

    /// Encode a bare recycle_alpha RuntimeCall.
    ///
    /// The ProxyCall chain extension wraps this in Proxy::proxy automatically.
    /// Only the inner SubtensorModule::recycle_alpha call is encoded here.
    ///
    /// recycle_alpha DESTROYS alpha tokens and reduces SubnetAlphaOut.
    ///
    /// # Arguments
    /// * `hotkey` - The hotkey to recycle alpha from
    /// * `amount` - Amount of alpha to recycle (u64)
    /// * `netuid` - Subnet ID
    pub fn recycle_alpha(
        hotkey: &AccountId,
        amount: u64,
        netuid: u16,
    ) -> Vec<u8> {
        let mut call_bytes = Vec::with_capacity(44);

        // SubtensorModule pallet index
        call_bytes.push(SUBTENSOR_MODULE_PALLET_INDEX);

        // recycle_alpha call variant index
        call_bytes.push(RECYCLE_ALPHA_CALL_INDEX);

        // hotkey: AccountId (32 bytes)
        call_bytes.extend_from_slice(hotkey.as_ref());

        // amount: u64 (8 bytes, little-endian)
        call_bytes.extend_from_slice(&amount.to_le_bytes());

        // netuid: u16 (2 bytes, little-endian)
        call_bytes.extend_from_slice(&netuid.to_le_bytes());

        call_bytes
    }
}
