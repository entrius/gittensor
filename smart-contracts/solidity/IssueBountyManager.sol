// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

/// @title IssueBountyManager - Issues Competition Smart Contract for Gittensor
contract IssueBountyManager {
    // ========================================================================
    // Constants
    // ========================================================================

    /// @notice Minimum bounty amount: 10 ALPHA (9 decimals)
    uint128 public constant MIN_BOUNTY = 10_000_000_000;

    /// @notice Default submission window: ~2 days at 12s blocks
    uint32 public constant DEFAULT_SUBMISSION_WINDOW_BLOCKS = 14400;

    /// @notice Default competition deadline: ~7 days at 12s blocks
    uint32 public constant DEFAULT_COMPETITION_DEADLINE_BLOCKS = 50400;

    /// @notice Default proposal expiry: ~3.3 hours at 12s blocks
    uint32 public constant DEFAULT_PROPOSAL_EXPIRY_BLOCKS = 1000;

    /// @notice Consensus threshold percentage (51% of stake required)
    uint8 public constant CONSENSUS_THRESHOLD_PERCENT = 51;

    // ========================================================================
    // Enums
    // ========================================================================

    /// @notice Status of an issue in its lifecycle
    enum IssueStatus {
        Registered,     // Issue registered, awaiting bounty fill
        Active,         // Issue has bounty filled, ready for competition
        InCompetition,  // Issue is currently in an active competition
        Completed,      // Issue has been completed (competition resolved)
        Cancelled       // Issue was cancelled by owner before competition
    }

    /// @notice Status of a competition
    enum CompetitionStatus {
        Active,     // Competition is active (miners working on solutions)
        Completed,  // Competition completed with a winner
        TimedOut,   // Competition timed out (no valid solution)
        Cancelled   // Competition cancelled (external solution or invalid)
    }

    // ========================================================================
    // Structs
    // ========================================================================

    /// @notice Represents a GitHub issue registered for competition
    struct Issue {
        uint64 id;
        bytes32 githubUrlHash;
        string repositoryFullName;
        uint32 issueNumber;
        uint128 bountyAmount;
        uint128 targetBounty;
        IssueStatus status;
        uint32 registeredAtBlock;
    }

    /// @notice Represents a head-to-head competition between two miners
    struct Competition {
        uint64 id;
        uint64 issueId;
        address miner1Hotkey;
        address miner2Hotkey;
        uint32 startBlock;
        uint32 submissionWindowEndBlock;
        uint32 deadlineBlock;
        CompetitionStatus status;
        address winnerHotkey;
        bytes32 winningPrUrlHash;
        uint128 payoutAmount;
    }

    /// @notice A proposal to pair two miners for a competition
    struct PairProposal {
        uint64 issueId;
        address miner1Hotkey;
        address miner2Hotkey;
        address proposer;
        uint32 proposedAtBlock;
        uint128 totalStakeVoted;
        uint256 votesCount;
    }

    /// @notice Votes for a solution winner in a competition
    struct SolutionVote {
        uint64 competitionId;
        address winnerHotkey;
        bytes32 prUrlHash;
        uint128 totalStakeVoted;
        uint256 votesCount;
    }

    /// @notice Votes for cancelling or timing out a competition
    struct CancelVote {
        uint64 competitionId;
        bytes32 reasonHash;
        uint128 totalStakeVoted;
        uint256 votesCount;
    }

    // Events
    event IssueRegistered(uint64 indexed issueId, bytes32 githubUrlHash, string repositoryFullName, uint32 issueNumber, uint128 targetBounty);
    event IssueCancelled(uint64 indexed issueId, uint128 returnedBounty);
    event PoolDeposit(address indexed depositor, uint128 amount);
    event CompetitionStarted(uint64 indexed competitionId, uint64 indexed issueId, address miner1Hotkey, address miner2Hotkey, uint32 deadlineBlock);
    event CompetitionCompleted(uint64 indexed competitionId, uint64 indexed issueId, address indexed winnerHotkey, uint128 payout, bytes32 prUrlHash);
    event CompetitionEnded(uint64 indexed competitionId, uint64 indexed issueId, uint8 status, bytes32 reasonHash);
    event PairVoteCast(uint64 indexed issueId, address indexed voter, uint128 stake);

    // ========================================================================
    // Errors
    // ========================================================================

    error NotOwner();
    error IssueNotFound();
    error IssueAlreadyExists();
    error BountyTooLow();
    error CannotCancel();
    error InvalidRepositoryName();
    error InvalidIssueNumber();
    error IssueNotActive();
    error MinerAlreadyInCompetition();
    error CompetitionNotFound();
    error CompetitionNotActive();
    error InvalidWinner();
    error SubmissionWindowNotEnded();
    error DeadlineNotPassed();
    error ProposalNotFound();
    error AlreadyVoted();
    error ProposalExpired();
    error InsufficientStake();
    error SameMiners();

    // ========================================================================
    // State Variables
    // ========================================================================

    /// @notice Contract owner with administrative privileges
    address public owner;

    /// @notice Treasury hotkey for staking operations
    address public treasuryHotkey;

    /// @notice Subnet ID for this contract
    uint16 public netuid;

    /// @notice Counter for generating unique issue IDs
    uint64 public nextIssueId;

    /// @notice Unallocated emissions storage (alpha pool)
    uint128 public alphaPool;

    /// @notice Counter for generating unique competition IDs
    uint64 public nextCompetitionId;

    /// @notice Submission window in blocks
    uint32 public submissionWindowBlocks;

    /// @notice Competition deadline in blocks
    uint32 public competitionDeadlineBlocks;

    /// @notice Proposal expiry in blocks
    uint32 public proposalExpiryBlocks;

    /// @notice Total network stake for consensus calculation
    uint128 public totalNetworkStake;

    /// @notice Mapping from issue ID to Issue struct
    mapping(uint64 => Issue) public issues;

    /// @notice Mapping from URL hash to issue ID for deduplication
    mapping(bytes32 => uint64) public urlHashToId;

    /// @notice FIFO queue of issue IDs awaiting bounty fill
    uint64[] public bountyQueue;

    /// @notice Mapping from competition ID to Competition struct
    mapping(uint64 => Competition) public competitions;

    /// @notice Mapping from issue ID to active competition ID
    mapping(uint64 => uint64) public issueToCompetition;

    /// @notice Mapping from miner hotkey to active competition ID
    mapping(address => uint64) public minerInCompetition;

    mapping(uint64 => PairProposal) public pairProposals;
    mapping(uint64 => bool) public hasPairProposal;
    mapping(uint64 => mapping(address => bool)) public pairProposalVoters;
    mapping(uint64 => SolutionVote) public solutionVotes;
    mapping(uint64 => bool) public hasSolutionVote;
    mapping(uint64 => mapping(address => bool)) public solutionVoteVoters;
    mapping(uint64 => CancelVote) public timeoutVotes;
    mapping(uint64 => bool) public hasTimeoutVote;
    mapping(uint64 => mapping(address => bool)) public timeoutVoteVoters;
    mapping(uint64 => CancelVote) public cancelVotes;
    mapping(uint64 => bool) public hasCancelVote;
    mapping(uint64 => mapping(address => bool)) public cancelVoteVoters;

    // ========================================================================
    // Modifiers
    // ========================================================================

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ========================================================================
    // Constructor
    // ========================================================================

    /**
     * @notice Creates a new IssueBountyManager contract
     * @param _owner Account with administrative privileges
     * @param _treasuryHotkey Hotkey for staking operations
     * @param _netuid Subnet ID (74 for mainnet, 422 for testnet)
     */
    constructor(address _owner, address _treasuryHotkey, uint16 _netuid) {
        owner = _owner;
        treasuryHotkey = _treasuryHotkey;
        netuid = _netuid;
        nextIssueId = 1;
        nextCompetitionId = 1;
        submissionWindowBlocks = DEFAULT_SUBMISSION_WINDOW_BLOCKS;
        competitionDeadlineBlocks = DEFAULT_COMPETITION_DEADLINE_BLOCKS;
        proposalExpiryBlocks = DEFAULT_PROPOSAL_EXPIRY_BLOCKS;
        totalNetworkStake = 0;
    }

    // ========================================================================
    // Issue Registry Functions
    // ========================================================================

    /**
     * @notice Registers a new GitHub issue for competition
     * @dev Only the contract owner can register issues
     * @param githubUrl Full GitHub issue URL
     * @param repositoryFullName Repository in "owner/repo" format
     * @param issueNumber Issue number within the repository
     * @param targetBounty Target bounty amount in ALPHA
     * @return issueId The assigned issue ID
     */
    function registerIssue(
        string calldata githubUrl,
        string calldata repositoryFullName,
        uint32 issueNumber,
        uint128 targetBounty
    ) external onlyOwner returns (uint64) {
        // Validate inputs
        if (targetBounty < MIN_BOUNTY) revert BountyTooLow();
        if (issueNumber == 0) revert InvalidIssueNumber();
        if (!_isValidRepoName(repositoryFullName)) revert InvalidRepositoryName();

        // Hash the URL for deduplication
        bytes32 urlHash = keccak256(bytes(githubUrl));

        // Check if URL already registered
        if (urlHashToId[urlHash] != 0) revert IssueAlreadyExists();

        // Get current block number
        uint32 currentBlock = uint32(block.number);

        // Create new issue
        uint64 issueId = nextIssueId;
        nextIssueId++;

        Issue storage newIssue = issues[issueId];
        newIssue.id = issueId;
        newIssue.githubUrlHash = urlHash;
        newIssue.repositoryFullName = repositoryFullName;
        newIssue.issueNumber = issueNumber;
        newIssue.bountyAmount = 0;
        newIssue.targetBounty = targetBounty;
        newIssue.status = IssueStatus.Registered;
        newIssue.registeredAtBlock = currentBlock;

        // Store URL hash mapping
        urlHashToId[urlHash] = issueId;

        // Add to bounty queue
        bountyQueue.push(issueId);

        // Emit event
        emit IssueRegistered(
            issueId,
            urlHash,
            repositoryFullName,
            issueNumber,
            targetBounty
        );

        // Try to fill bounties from pool
        _fillBounties();

        return issueId;
    }

    /**
     * @notice Cancels an issue before it enters competition
     * @dev Only the contract owner can cancel issues. Any allocated bounty is returned to the alpha pool.
     * @param issueId ID of the issue to cancel
     */
    function cancelIssue(uint64 issueId) external onlyOwner {
        Issue storage issue = issues[issueId];
        if (issue.id == 0) revert IssueNotFound();

        if (!_isModifiable(issue.status)) revert CannotCancel();

        uint128 returnedBounty = issue.bountyAmount;

        // Return bounty to pool
        alphaPool += returnedBounty;

        // Update issue status
        issue.status = IssueStatus.Cancelled;
        issue.bountyAmount = 0;

        // Remove from bounty queue
        _removeFromBountyQueue(issueId);

        // Emit event
        emit IssueCancelled(issueId, returnedBounty);
    }

    // ========================================================================
    // Bounty Pool Functions
    // ========================================================================

    /// @notice Deposits funds to the alpha pool (also used for emissions)
    function depositToPool() external payable {
        uint128 amount = uint128(msg.value);
        if (amount == 0) return;
        alphaPool += amount;
        emit PoolDeposit(msg.sender, amount);
        _fillBounties();
    }

    /// @notice Alias for depositToPool (backwards compatibility for emissions)
    function receiveEmissions() external payable {
        uint128 amount = uint128(msg.value);
        if (amount == 0) return;
        alphaPool += amount;
        emit PoolDeposit(msg.sender, amount);
        _fillBounties();
    }

    /**
     * @dev Internal function to fill bounties from the alpha pool using FIFO order
     */
    function _fillBounties() internal {
        uint256 queueLength = bountyQueue.length;
        uint256 i = 0;

        while (i < queueLength && alphaPool > 0) {
            uint64 issueId = bountyQueue[i];
            Issue storage issue = issues[issueId];

            // Skip issues that can't receive bounty
            if (!_isModifiable(issue.status)) {
                // Remove from queue by swapping with last and popping
                if (i < queueLength - 1) {
                    bountyQueue[i] = bountyQueue[queueLength - 1];
                }
                bountyQueue.pop();
                queueLength--;
                continue;
            }

            uint128 remaining = issue.targetBounty - issue.bountyAmount;
            if (remaining == 0) {
                // Remove from queue
                if (i < queueLength - 1) {
                    bountyQueue[i] = bountyQueue[queueLength - 1];
                }
                bountyQueue.pop();
                queueLength--;
                continue;
            }

            // Fill up to the remaining amount or available pool
            uint128 fillAmount = remaining < alphaPool ? remaining : alphaPool;

            issue.bountyAmount += fillAmount;
            alphaPool -= fillAmount;

            bool isFullyFunded = issue.bountyAmount >= issue.targetBounty;

            // Update status if fully funded
            if (isFullyFunded) {
                issue.status = IssueStatus.Active;
                // Remove from queue
                if (i < queueLength - 1) {
                    bountyQueue[i] = bountyQueue[queueLength - 1];
                }
                bountyQueue.pop();
                queueLength--;
            } else {
                i++;
            }

        }
    }

    // ========================================================================
    // Validator Consensus Functions
    // ========================================================================

    /**
     * @notice Proposes a pair of miners for a competition on an issue
     * @dev Creates a new pair proposal or replaces an existing one
     * @param issueId The issue to start a competition for
     * @param miner1Hotkey First miner's hotkey
     * @param miner2Hotkey Second miner's hotkey
     */
    function proposePair(
        uint64 issueId,
        address miner1Hotkey,
        address miner2Hotkey
    ) external {
        // Validate miners are different
        if (miner1Hotkey == miner2Hotkey) revert SameMiners();

        // Validate issue exists and is Active
        Issue storage issue = issues[issueId];
        if (issue.id == 0) revert IssueNotFound();
        if (issue.status != IssueStatus.Active) revert IssueNotActive();

        // Validate miners are not already in a competition
        if (minerInCompetition[miner1Hotkey] != 0) revert MinerAlreadyInCompetition();
        if (minerInCompetition[miner2Hotkey] != 0) revert MinerAlreadyInCompetition();

        // Get caller's stake
        uint128 stake = _getValidatorStake(msg.sender);
        if (stake == 0) revert InsufficientStake();

        uint32 currentBlock = uint32(block.number);

        // Create new proposal (replaces any existing one)
        PairProposal storage proposal = pairProposals[issueId];
        proposal.issueId = issueId;
        proposal.miner1Hotkey = miner1Hotkey;
        proposal.miner2Hotkey = miner2Hotkey;
        proposal.proposer = msg.sender;
        proposal.proposedAtBlock = currentBlock;
        proposal.totalStakeVoted = stake;
        proposal.votesCount = 1;
        hasPairProposal[issueId] = true;
        pairProposalVoters[issueId][msg.sender] = true;

        // Emit events
        emit PairVoteCast(issueId, msg.sender, stake);

        // Check if consensus reached
        if (_checkConsensus(proposal.totalStakeVoted)) {
            // Start competition immediately
            _startCompetition(issueId, miner1Hotkey, miner2Hotkey);
            // Clear the proposal
            _clearPairProposal(issueId);
        }
    }

    /**
     * @notice Votes on an existing pair proposal
     * @dev Adds the caller's stake-weighted vote to the proposal
     * @param issueId The issue with an active proposal
     */
    function votePair(uint64 issueId) external {
        // Get existing proposal
        if (!hasPairProposal[issueId]) revert ProposalNotFound();
        PairProposal storage proposal = pairProposals[issueId];

        uint32 currentBlock = uint32(block.number);

        // Check if proposal has expired
        if (currentBlock > proposal.proposedAtBlock + proposalExpiryBlocks) {
            _clearPairProposal(issueId);
            revert ProposalExpired();
        }

        // Check validator hasn't already voted
        if (pairProposalVoters[issueId][msg.sender]) revert AlreadyVoted();

        // Validate issue is still Active
        Issue storage issue = issues[issueId];
        if (issue.status != IssueStatus.Active) revert IssueNotActive();

        // Get caller's stake
        uint128 stake = _getValidatorStake(msg.sender);
        if (stake == 0) revert InsufficientStake();

        // Add vote
        pairProposalVoters[issueId][msg.sender] = true;
        proposal.totalStakeVoted += stake;
        proposal.votesCount++;

        // Emit event
        emit PairVoteCast(issueId, msg.sender, stake);

        // Check if consensus reached
        if (_checkConsensus(proposal.totalStakeVoted)) {
            // Start competition
            _startCompetition(issueId, proposal.miner1Hotkey, proposal.miner2Hotkey);
            // Clear the proposal
            _clearPairProposal(issueId);
        }
    }

    /**
     * @notice Votes for a solution winner in an active competition
     * @dev Can only be called after the submission window ends
     * @param competitionId The competition to vote on
     * @param winnerHotkey The proposed winner's hotkey
     * @param prUrlHash Hash of the winning PR URL
     */
    function voteSolution(
        uint64 competitionId,
        address winnerHotkey,
        bytes32 prUrlHash
    ) external {
        // Get competition
        Competition storage competition = competitions[competitionId];
        if (competition.id == 0) revert CompetitionNotFound();

        // Validate competition is active
        if (competition.status != CompetitionStatus.Active) revert CompetitionNotActive();

        // Validate winner is a participant
        if (winnerHotkey != competition.miner1Hotkey && winnerHotkey != competition.miner2Hotkey) {
            revert InvalidWinner();
        }

        // Validate submission window has ended
        uint32 currentBlock = uint32(block.number);
        if (currentBlock <= competition.submissionWindowEndBlock) revert SubmissionWindowNotEnded();

        // Check if voter already voted
        if (solutionVoteVoters[competitionId][msg.sender]) revert AlreadyVoted();

        // Get caller's stake
        uint128 stake = _getValidatorStake(msg.sender);
        if (stake == 0) revert InsufficientStake();

        // Get or create solution vote tracking
        if (!hasSolutionVote[competitionId]) {
            SolutionVote storage sv = solutionVotes[competitionId];
            sv.competitionId = competitionId;
            sv.winnerHotkey = winnerHotkey;
            sv.prUrlHash = prUrlHash;
            sv.totalStakeVoted = 0;
            sv.votesCount = 0;
            hasSolutionVote[competitionId] = true;
        }

        SolutionVote storage solutionVote = solutionVotes[competitionId];

        // Add vote
        solutionVoteVoters[competitionId][msg.sender] = true;
        solutionVote.totalStakeVoted += stake;
        solutionVote.votesCount++;

        // Emit event

        // Check consensus
        if (_checkConsensus(solutionVote.totalStakeVoted)) {
            // Complete competition with winner
            _completeCompetition(competitionId, winnerHotkey, prUrlHash);
            // Clear solution votes
            _clearSolutionVote(competitionId);
        }
    }

    /**
     * @notice Votes to time out a competition that has passed its deadline
     * @dev Can only be called after the competition deadline
     * @param competitionId The competition to time out
     */
    function voteTimeout(uint64 competitionId) external {
        // Get competition
        Competition storage competition = competitions[competitionId];
        if (competition.id == 0) revert CompetitionNotFound();

        // Validate competition is active
        if (competition.status != CompetitionStatus.Active) revert CompetitionNotActive();

        // Validate deadline has passed
        uint32 currentBlock = uint32(block.number);
        if (currentBlock <= competition.deadlineBlock) revert DeadlineNotPassed();

        // Check if voter already voted
        if (timeoutVoteVoters[competitionId][msg.sender]) revert AlreadyVoted();

        // Get caller's stake
        uint128 stake = _getValidatorStake(msg.sender);
        if (stake == 0) revert InsufficientStake();

        bytes32 reasonHash = bytes32(0); // Empty reason for timeout

        // Get or create timeout vote tracking
        if (!hasTimeoutVote[competitionId]) {
            CancelVote storage tv = timeoutVotes[competitionId];
            tv.competitionId = competitionId;
            tv.reasonHash = reasonHash;
            tv.totalStakeVoted = 0;
            tv.votesCount = 0;
            hasTimeoutVote[competitionId] = true;
        }

        CancelVote storage timeoutVote = timeoutVotes[competitionId];

        // Add vote
        timeoutVoteVoters[competitionId][msg.sender] = true;
        timeoutVote.totalStakeVoted += stake;
        timeoutVote.votesCount++;

        // Emit event

        // Check consensus
        if (_checkConsensus(timeoutVote.totalStakeVoted)) {
            // Time out the competition
            _timeoutCompetition(competitionId);
            // Clear timeout votes
            _clearTimeoutVote(competitionId);
        }
    }

    /**
     * @notice Votes to cancel a competition (e.g., external solution found)
     * @dev Can be called at any time during an active competition
     * @param competitionId The competition to cancel
     * @param reasonHash Hash of the cancellation reason
     */
    function voteCancel(uint64 competitionId, bytes32 reasonHash) external {
        // Get competition
        Competition storage competition = competitions[competitionId];
        if (competition.id == 0) revert CompetitionNotFound();

        // Validate competition is active
        if (competition.status != CompetitionStatus.Active) revert CompetitionNotActive();

        // Check if voter already voted
        if (cancelVoteVoters[competitionId][msg.sender]) revert AlreadyVoted();

        // Get caller's stake
        uint128 stake = _getValidatorStake(msg.sender);
        if (stake == 0) revert InsufficientStake();

        // Get or create cancel vote tracking
        if (!hasCancelVote[competitionId]) {
            CancelVote storage cv = cancelVotes[competitionId];
            cv.competitionId = competitionId;
            cv.reasonHash = reasonHash;
            cv.totalStakeVoted = 0;
            cv.votesCount = 0;
            hasCancelVote[competitionId] = true;
        }

        CancelVote storage cancelVote = cancelVotes[competitionId];

        // Add vote
        cancelVoteVoters[competitionId][msg.sender] = true;
        cancelVote.totalStakeVoted += stake;
        cancelVote.votesCount++;

        // Emit event

        // Check consensus
        if (_checkConsensus(cancelVote.totalStakeVoted)) {
            // Cancel the competition
            _cancelCompetition(competitionId, reasonHash);
            // Clear cancel votes
            _clearCancelVote(competitionId);
        }
    }

    // ========================================================================
    // Admin Functions
    // ========================================================================

    function setOwner(address newOwner) external onlyOwner {
        owner = newOwner;
    }

    function setTreasuryHotkey(address newHotkey) external onlyOwner {
        treasuryHotkey = newHotkey;
    }

    function setTotalNetworkStake(uint128 stake) external onlyOwner {
        totalNetworkStake = stake;
    }

    /**
     * @notice Sets competition timing configuration
     * @dev Only the owner can call this
     * @param _submissionWindowBlocks Blocks for submission window
     * @param _competitionDeadlineBlocks Blocks for competition deadline
     * @param _proposalExpiryBlocks Blocks until proposals expire
     */
    function setCompetitionConfig(
        uint32 _submissionWindowBlocks,
        uint32 _competitionDeadlineBlocks,
        uint32 _proposalExpiryBlocks
    ) external onlyOwner {
        submissionWindowBlocks = _submissionWindowBlocks;
        competitionDeadlineBlocks = _competitionDeadlineBlocks;
        proposalExpiryBlocks = _proposalExpiryBlocks;
    }

    // ========================================================================
    // Internal Functions
    // ========================================================================

    function _isValidRepoName(string calldata name) internal pure returns (bool) {
        bytes memory b = bytes(name);
        uint256 slash = 0;
        for (uint256 i = 0; i < b.length; i++) {
            if (b[i] == "/") {
                if (slash > 0 || i == 0) return false;
                slash = i;
            }
        }
        return slash > 0 && slash < b.length - 1;
    }

    function _isModifiable(IssueStatus status) internal pure returns (bool) {
        return status == IssueStatus.Registered || status == IssueStatus.Active;
    }

    /**
     * @dev Removes an issue from the bounty queue
     */
    function _removeFromBountyQueue(uint64 issueId) internal {
        uint256 length = bountyQueue.length;
        for (uint256 i = 0; i < length; i++) {
            if (bountyQueue[i] == issueId) {
                if (i < length - 1) {
                    bountyQueue[i] = bountyQueue[length - 1];
                }
                bountyQueue.pop();
                break;
            }
        }
    }

    function _getValidatorStake(address) internal view returns (uint128) {
        if (totalNetworkStake > 0) {
            return totalNetworkStake / 10;
        }
        return 1_000_000_000_000;
    }

    function _checkConsensus(uint128 totalVoted) internal view returns (bool) {
        if (totalNetworkStake == 0) return false;

        // Calculate 51% threshold
        uint128 threshold = (totalNetworkStake * CONSENSUS_THRESHOLD_PERCENT) / 100;

        return totalVoted > threshold;
    }

    /**
     * @dev Starts a competition from a pair proposal
     */
    function _startCompetition(
        uint64 issueId,
        address miner1Hotkey,
        address miner2Hotkey
    ) internal returns (uint64) {
        uint32 currentBlock = uint32(block.number);
        uint64 competitionId = nextCompetitionId;
        nextCompetitionId++;

        // Create competition
        Competition storage competition = competitions[competitionId];
        competition.id = competitionId;
        competition.issueId = issueId;
        competition.miner1Hotkey = miner1Hotkey;
        competition.miner2Hotkey = miner2Hotkey;
        competition.startBlock = currentBlock;
        competition.submissionWindowEndBlock = currentBlock + submissionWindowBlocks;
        competition.deadlineBlock = currentBlock + competitionDeadlineBlocks;
        competition.status = CompetitionStatus.Active;
        competition.winnerHotkey = address(0);
        competition.winningPrUrlHash = bytes32(0);
        competition.payoutAmount = 0;

        // Store mappings
        issueToCompetition[issueId] = competitionId;
        minerInCompetition[miner1Hotkey] = competitionId;
        minerInCompetition[miner2Hotkey] = competitionId;

        // Update issue status
        issues[issueId].status = IssueStatus.InCompetition;

        // Emit event
        emit CompetitionStarted(
            competitionId,
            issueId,
            miner1Hotkey,
            miner2Hotkey,
            competition.deadlineBlock
        );

        return competitionId;
    }

    /**
     * @dev Completes a competition with a winner
     */
    function _completeCompetition(
        uint64 competitionId,
        address winner,
        bytes32 prHash
    ) internal {
        Competition storage competition = competitions[competitionId];
        uint64 issueId = competition.issueId;
        Issue storage issue = issues[issueId];
        uint128 payout = issue.bountyAmount;

        // Update competition
        competition.status = CompetitionStatus.Completed;
        competition.winnerHotkey = winner;
        competition.winningPrUrlHash = prHash;
        competition.payoutAmount = payout;

        // Update issue
        issue.status = IssueStatus.Completed;
        issue.bountyAmount = 0; // Bounty paid out

        // Clear miner tracking
        delete minerInCompetition[competition.miner1Hotkey];
        delete minerInCompetition[competition.miner2Hotkey];
        delete issueToCompetition[issueId];

        // Emit event
        emit CompetitionCompleted(
            competitionId,
            issueId,
            winner,
            payout,
            prHash
        );
    }

    /**
     * @dev Times out a competition, returning bounty to pool
     */
    function _timeoutCompetition(uint64 competitionId) internal {
        Competition storage competition = competitions[competitionId];
        uint64 issueId = competition.issueId;
        competition.status = CompetitionStatus.TimedOut;
        issues[issueId].status = IssueStatus.Active;
        delete minerInCompetition[competition.miner1Hotkey];
        delete minerInCompetition[competition.miner2Hotkey];
        delete issueToCompetition[issueId];
        emit CompetitionEnded(competitionId, issueId, 2, bytes32(0));
    }

    function _cancelCompetition(uint64 competitionId, bytes32 reasonHash) internal {
        Competition storage competition = competitions[competitionId];
        uint64 issueId = competition.issueId;
        Issue storage issue = issues[issueId];
        uint128 recycledAmount = issue.bountyAmount;
        competition.status = CompetitionStatus.Cancelled;
        issue.status = IssueStatus.Completed;
        issue.bountyAmount = 0;
        alphaPool += recycledAmount;
        delete minerInCompetition[competition.miner1Hotkey];
        delete minerInCompetition[competition.miner2Hotkey];
        delete issueToCompetition[issueId];
        emit CompetitionEnded(competitionId, issueId, 3, reasonHash); // 3 = Cancelled
    }

    /**
     * @dev Clears pair proposal data
     */
    function _clearPairProposal(uint64 issueId) internal {
        delete pairProposals[issueId];
        hasPairProposal[issueId] = false;
    }

    /**
     * @dev Clears solution vote data
     */
    function _clearSolutionVote(uint64 competitionId) internal {
        delete solutionVotes[competitionId];
        hasSolutionVote[competitionId] = false;
    }

    /**
     * @dev Clears timeout vote data
     */
    function _clearTimeoutVote(uint64 competitionId) internal {
        delete timeoutVotes[competitionId];
        hasTimeoutVote[competitionId] = false;
    }

    /**
     * @dev Clears cancel vote data
     */
    function _clearCancelVote(uint64 competitionId) internal {
        delete cancelVotes[competitionId];
        hasCancelVote[competitionId] = false;
    }

}
