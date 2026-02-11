use ink::prelude::vec::Vec;
use ink::primitives::AccountId;
use scale::{Encode, Output};

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
