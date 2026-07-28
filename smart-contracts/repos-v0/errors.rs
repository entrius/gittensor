use scale::{Decode, Encode};

/// Errors that can occur in the RepoRegistry contract
#[derive(Debug, PartialEq, Eq, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub enum Error {
    /// Caller is not the contract owner
    NotOwner,
    /// Caller is not the repository owner
    NotRepoOwner,
    /// Caller is not a whitelisted validator hotkey
    NotWhitelistedVoter,
    /// Contract is paused
    Paused,
    /// Repository not found
    RepoNotFound,
    /// Repository exists but is not active
    RepoNotActive,
    /// Repository is already registered and active
    AlreadyRegistered,
    /// GitHub id must be non-zero
    InvalidGithubId,
    /// Full name must be lowercase "owner/repo"
    InvalidFullName,
    /// Zero address not allowed
    ZeroAddress,
    /// Registry full and every repo is inside its immunity window
    NoPrunableSlot,
    /// Per-block registration cap reached
    TooManyRegistrationsThisBlock,
    /// No bounds registered for this param key
    UnknownParamKey,
    /// Value outside the allowed bounds
    ValueOutOfBounds,
    /// Change rate limit not yet elapsed
    RateLimited,
    /// Label is empty, too long, or contains control characters
    InvalidLabel,
    /// Label multiplier cap reached
    TooManyLabels,
    /// Label multiplier not found
    LabelNotFound,
    /// Branch pattern fails validation
    InvalidPattern,
    /// Branch pattern cap reached
    TooManyPatterns,
    /// Duplicate branch pattern
    DuplicatePattern,
    /// Basket must not be empty
    EmptyBasket,
    /// Basket exceeds the basket cap
    BasketTooLarge,
    /// Duplicate repo id in basket
    DuplicateBasketEntry,
    /// Basket weights must be non-zero
    ZeroWeight,
    /// Basket weights must sum to 65535
    WeightSumMismatch,
    /// Voter already whitelisted
    VoterAlreadyAdded,
    /// Voter not in whitelist
    VoterNotFound,
    /// Voter whitelist cap reached
    TooManyVoters,
    /// Bounds must satisfy min <= max
    InvalidBounds,
    /// Param bounds key cap reached
    TooManyParamKeys,
    /// Constants failed validation
    InvalidConfig,
    /// Fee stake transfer failed
    FeeTransferFailed,
    /// Fee stake delta shortfall (silent cap detected)
    FeeShortfall,
    /// Fee recycle failed
    RecycleFailed,
    /// set_code_hash failed
    SetCodeFailed,
}
