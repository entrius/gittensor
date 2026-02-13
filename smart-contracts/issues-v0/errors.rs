use scale::{Decode, Encode};

/// Errors that can occur in the IssueBountyManager contract
#[derive(Debug, PartialEq, Eq, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub enum Error {
    /// Caller is not the contract owner
    NotOwner,
    /// Issue with the given ID does not exist
    IssueNotFound,
    /// Issue with the same URL already exists
    IssueAlreadyExists,
    /// Bounty amount is below minimum (10 ALPHA)
    BountyTooLow,
    /// Issue cannot be cancelled in its current state
    CannotCancel,
    /// Repository name is invalid (must be "owner/repo" format)
    InvalidRepositoryName,
    /// Issue number must be greater than zero
    InvalidIssueNumber,
    /// Issue is not in Active status
    IssueNotActive,
    /// Solver is not a valid miner (bronze+ tier required)
    InvalidSolver,
    /// Caller has already voted on this proposal
    AlreadyVoted,
    /// Caller has insufficient stake to vote
    InsufficientStake,
    /// Bounty has not been completed yet
    BountyNotCompleted,
    /// Bounty has no funds allocated
    BountyNotFunded,
    /// Stake transfer operation failed
    TransferFailed,
    /// Chain extension call failed
    ChainExtensionFailed,
    /// Recycling emissions failed during harvest
    RecyclingFailed,
    /// Issue has already been finalized (Completed or Cancelled)
    IssueAlreadyFinalized,
    /// No solver was set on the completed issue (should not happen)
    NoSolverSet,
    /// Bounty has already been paid out
    BountyAlreadyPaid,
    /// Validator already included as a voter
    ValidatorAlreadyWhitelisted,
    // Validator doesn't exist in whitelist
    ValidatorNotWhitelisted,
}
