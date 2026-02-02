use scale::{Decode, Encode};

/// Errors that can occur in the IssueBountyManager contract (v0 - no competitions)
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
    // MinerAlreadyInCompetition - REMOVED in v0 (no competitions)
    // CompetitionNotFound - REMOVED in v0 (no competitions)
    // CompetitionNotActive - REMOVED in v0 (no competitions)
    /// Solver is not a valid miner (bronze+ tier required)
    InvalidSolver,
    // SubmissionWindowNotEnded - REMOVED in v0 (no competitions)
    // DeadlineNotPassed - REMOVED in v0 (no competitions)
    // ProposalNotFound - REMOVED in v0 (no competitions)
    /// Caller has already voted on this proposal
    AlreadyVoted,
    // ProposalExpired - REMOVED in v0 (no competitions)
    /// Caller has insufficient stake to vote
    InsufficientStake,
    // SameMiners - REMOVED in v0 (no competitions)
    // BountyNotFound - REMOVED in v0 (unused, IssueNotFound used instead)
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
    // IssueNotFundable - REMOVED in v0 (unused, fill_bounties skips silently)
    // BountyAlreadyFunded - REMOVED in v0 (unused, fill_bounties skips silently)
    /// Issue has already been finalized (Completed or Cancelled)
    IssueAlreadyFinalized,
}
