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
    /// Miner is already participating in another competition
    MinerAlreadyInCompetition,
    /// Competition with the given ID does not exist
    CompetitionNotFound,
    /// Competition is not in Active status
    CompetitionNotActive,
    /// Winner is not a participant in this competition
    InvalidWinner,
    /// Submission window has not ended yet
    SubmissionWindowNotEnded,
    /// Competition deadline has not passed yet
    DeadlineNotPassed,
    /// No pair proposal exists for this issue
    ProposalNotFound,
    /// Caller has already voted on this proposal
    AlreadyVoted,
    /// Pair proposal has expired
    ProposalExpired,
    /// Caller has insufficient stake to vote
    InsufficientStake,
    /// Both miners in pair proposal are the same
    SameMiners,
    /// Bounty not found for the given issue ID
    BountyNotFound,
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
    /// Issue cannot be funded in its current state (not Registered)
    IssueNotFundable,
    /// Bounty is already fully funded
    BountyAlreadyFunded,
    /// Issue has already been finalized (Completed or Cancelled)
    IssueAlreadyFinalized,
}
