use super::*;

fn default_accounts() -> ink::env::test::DefaultAccounts<ink::env::DefaultEnvironment> {
    ink::env::test::default_accounts::<ink::env::DefaultEnvironment>()
}

fn set_caller(caller: AccountId) {
    ink::env::test::set_caller::<ink::env::DefaultEnvironment>(caller);
}

#[ink::test]
fn test_constructor() {
    let accounts = default_accounts();
    let contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    assert_eq!(contract.owner(), accounts.alice);
    assert_eq!(contract.treasury_hotkey(), accounts.bob);
    assert_eq!(contract.netuid(), 74);
    assert_eq!(contract.next_issue_id(), 1);
    assert_eq!(contract.next_competition_id(), 1);
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn test_register_issue() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    );

    assert!(result.is_ok());
    assert_eq!(result.unwrap(), 1);
    assert_eq!(contract.next_issue_id(), 2);

    let issue = contract.get_issue(1);
    assert!(issue.is_some());
    let issue = issue.unwrap();
    assert_eq!(issue.id, 1);
    assert_eq!(issue.issue_number, 1);
    assert_eq!(issue.status, IssueStatus::Registered);
}

#[ink::test]
fn test_register_issue_not_owner() {
    let accounts = default_accounts();
    set_caller(accounts.bob);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    );

    assert_eq!(result, Err(Error::NotOwner));
}

#[ink::test]
fn test_register_issue_bounty_too_low() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY.saturating_sub(1),
    );

    assert_eq!(result, Err(Error::BountyTooLow));
}

#[ink::test]
fn test_register_issue_invalid_repo_name() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.register_issue(
        String::from("https://github.com/test/issues/1"),
        String::from("testrepo"),
        1,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(Error::InvalidRepositoryName));

    let result = contract.register_issue(
        String::from("https://github.com/test/issues/1"),
        String::from("/repo"),
        1,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(Error::InvalidRepositoryName));

    let result = contract.register_issue(
        String::from("https://github.com/test/issues/1"),
        String::from("test/"),
        1,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(Error::InvalidRepositoryName));
}

#[ink::test]
fn test_is_valid_repo_name() {
    let accounts = default_accounts();
    let contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    assert!(contract.is_valid_repo_name("owner/repo"));
    assert!(contract.is_valid_repo_name("test/test"));
    assert!(!contract.is_valid_repo_name("noslash"));
    assert!(!contract.is_valid_repo_name("/startwithslash"));
    assert!(!contract.is_valid_repo_name("endwithslash/"));
    assert!(!contract.is_valid_repo_name("multiple/slashes/here"));
}

#[ink::test]
fn test_cancel_issue() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let issue_id = contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let result = contract.cancel_issue(issue_id);
    assert!(result.is_ok());

    let issue = contract.get_issue(issue_id).unwrap();
    assert_eq!(issue.status, IssueStatus::Cancelled);
}

#[ink::test]
fn test_set_owner() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    assert_eq!(contract.owner(), accounts.alice);

    let result = contract.set_owner(accounts.charlie);
    assert!(result.is_ok());
    assert_eq!(contract.owner(), accounts.charlie);
}

#[ink::test]
fn test_get_issues_by_status() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/2"),
            String::from("test/repo"),
            2,
            MIN_BOUNTY,
        )
        .unwrap();

    let registered = contract.get_issues_by_status(IssueStatus::Registered);
    assert_eq!(registered.len(), 2);

    let active = contract.get_issues_by_status(IssueStatus::Active);
    assert_eq!(active.len(), 0);
}

// ================================================================
// Voting Validation Tests
// ================================================================

#[ink::test]
fn test_validate_active_competition_not_found() {
    let accounts = default_accounts();
    let contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.validate_active_competition(999);
    assert_eq!(result, Err(Error::CompetitionNotFound));
}

#[ink::test]
fn test_check_consensus_threshold() {
    let accounts = default_accounts();
    let contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Below threshold (1 vote required)
    assert!(!contract.check_consensus(0));
    // At threshold
    assert!(contract.check_consensus(REQUIRED_VALIDATOR_VOTES));
    // Above threshold
    assert!(contract.check_consensus(REQUIRED_VALIDATOR_VOTES + 1));
}

#[ink::test]
fn test_check_not_voted_solution() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Initially not voted
    let result = contract.check_not_voted_solution(1, accounts.bob);
    assert!(result.is_ok());

    // Mark as voted
    contract.solution_vote_voters.insert((1, accounts.bob), &true);

    // Now should return AlreadyVoted error
    let result = contract.check_not_voted_solution(1, accounts.bob);
    assert_eq!(result, Err(Error::AlreadyVoted));

    // Different user should still be able to vote
    let result = contract.check_not_voted_solution(1, accounts.charlie);
    assert!(result.is_ok());
}

// ================================================================
// Bounty Pool Tests
// ================================================================

#[ink::test]
fn test_fill_bounties_fifo_order() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register two issues
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/2"),
            String::from("test/repo"),
            2,
            MIN_BOUNTY * 2,
        )
        .unwrap();

    // Add partial funds (only enough for first issue)
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // First issue should be filled and active
    let issue1 = contract.get_issue(1).unwrap();
    assert_eq!(issue1.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue1.status, IssueStatus::Active);

    // Second issue should still be registered with no bounty
    let issue2 = contract.get_issue(2).unwrap();
    assert_eq!(issue2.bounty_amount, 0);
    assert_eq!(issue2.status, IssueStatus::Registered);

    // Pool should be empty
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn test_fill_bounties_partial_fill() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register issue with large target
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY * 3,
        )
        .unwrap();

    // Add partial funds
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Issue should be partially filled but still Registered
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue.status, IssueStatus::Registered);

    // Add more funds to complete it
    contract.alpha_pool = MIN_BOUNTY * 2;
    contract.fill_bounties();

    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.bounty_amount, MIN_BOUNTY * 3);
    assert_eq!(issue.status, IssueStatus::Active);
}

// ================================================================
// Competition State Tests
// ================================================================

#[ink::test]
fn test_start_competition_state_changes() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register and fill an issue
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Start competition manually (simulating consensus)
    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);

    // Verify competition was created
    let comp = contract.get_competition(comp_id).unwrap();
    assert_eq!(comp.id, 1);
    assert_eq!(comp.issue_id, 1);
    assert_eq!(comp.miner1_hotkey, accounts.bob);
    assert_eq!(comp.miner2_hotkey, accounts.charlie);
    assert_eq!(comp.status, CompetitionStatus::Active);

    // Verify issue status changed
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.status, IssueStatus::InCompetition);

    // Verify miners are tracked
    assert!(contract.is_miner_in_competition(accounts.bob));
    assert!(contract.is_miner_in_competition(accounts.charlie));
    assert_eq!(contract.get_miner_competition(accounts.bob), comp_id);
}

// NOTE: This test is ignored because complete_competition now uses call_runtime
// for auto-payout, which is not supported in off-chain tests.
#[ink::test]
#[ignore = "complete_competition uses call_runtime for auto-payout"]
fn test_complete_competition_state_changes() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Setup: register, fill, and start competition
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);

    // Complete the competition (winner_coldkey = accounts.bob for test)
    let pr_hash = [1u8; 32];
    contract.complete_competition(comp_id, accounts.bob, pr_hash, accounts.bob);

    // Verify competition state
    let comp = contract.get_competition(comp_id).unwrap();
    assert_eq!(comp.status, CompetitionStatus::Completed);
    assert_eq!(comp.winner_hotkey, accounts.bob);
    assert_eq!(comp.winning_pr_url_hash, pr_hash);
    assert_eq!(comp.payout_amount, MIN_BOUNTY);

    // Verify issue completed
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.status, IssueStatus::Completed);
    assert_eq!(issue.bounty_amount, 0);

    // Verify miners released
    assert!(!contract.is_miner_in_competition(accounts.bob));
    assert!(!contract.is_miner_in_competition(accounts.charlie));
}

#[ink::test]
fn test_timeout_competition_returns_to_active() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Setup: register, fill, and start competition
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);

    // Timeout the competition
    contract.timeout_competition(comp_id);

    // Verify competition timed out
    let comp = contract.get_competition(comp_id).unwrap();
    assert_eq!(comp.status, CompetitionStatus::TimedOut);

    // Issue should return to Active (can be re-competed)
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.status, IssueStatus::Active);

    // Miners released
    assert!(!contract.is_miner_in_competition(accounts.bob));
}

#[ink::test]
#[ignore = "execute_cancel_issue uses recycle() which calls call_runtime (not supported in off-chain tests)"]
fn test_execute_cancel_issue_recycles_bounty() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Setup: register, fill, and start competition
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    assert_eq!(contract.get_alpha_pool(), 0);

    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);

    // Cancel the issue (cascades to competition)
    let reason_hash = [2u8; 32];
    contract.execute_cancel_issue(1, reason_hash);

    // Verify competition cancelled
    let comp = contract.get_competition(comp_id).unwrap();
    assert_eq!(comp.status, CompetitionStatus::Cancelled);

    // Bounty should be in alpha pool (recycle fails in off-chain tests, falls back to pool)
    assert_eq!(contract.get_alpha_pool(), MIN_BOUNTY);

    // Issue marked Cancelled (not Completed - unified cancel behavior)
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.status, IssueStatus::Cancelled);
    assert_eq!(issue.bounty_amount, 0);
}

// ================================================================
// Vote Storage Tests
// ================================================================

#[ink::test]
fn test_get_or_create_solution_vote_creates_new() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let pr_hash = [1u8; 32];
    let vote = contract.get_or_create_solution_vote(1, accounts.bob, pr_hash, accounts.bob);

    assert_eq!(vote.competition_id, 1);
    assert_eq!(vote.winner_hotkey, accounts.bob);
    assert_eq!(vote.winner_coldkey, accounts.bob);
    assert_eq!(vote.pr_url_hash, pr_hash);
    assert_eq!(vote.total_stake_voted, 0);
    assert_eq!(vote.votes_count, 0);
}

#[ink::test]
fn test_get_or_create_solution_vote_returns_existing() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create and store initial vote
    let pr_hash = [1u8; 32];
    let mut vote = contract.get_or_create_solution_vote(1, accounts.bob, pr_hash, accounts.bob);
    vote.total_stake_voted = 1000;
    vote.votes_count = 5;
    contract.solution_votes.insert(1, &vote);

    // Get existing vote (different params should be ignored)
    let vote2 = contract.get_or_create_solution_vote(1, accounts.charlie, [2u8; 32], accounts.charlie);

    // Should return existing vote data
    assert_eq!(vote2.winner_hotkey, accounts.bob);
    assert_eq!(vote2.winner_coldkey, accounts.bob);
    assert_eq!(vote2.total_stake_voted, 1000);
    assert_eq!(vote2.votes_count, 5);
}

#[ink::test]
fn test_clear_solution_vote() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create a vote
    let vote = SolutionVote {
        competition_id: 1,
        winner_hotkey: accounts.bob,
        winner_coldkey: accounts.bob,
        pr_url_hash: [1u8; 32],
        total_stake_voted: 1000,
        votes_count: 5,
    };
    contract.solution_votes.insert(1, &vote);

    // Clear it
    contract.clear_solution_vote(1);

    // Verify cleared
    assert!(contract.solution_votes.get(1).is_none());
}

// ================================================================
// Pair Proposal Tests
// ================================================================

#[ink::test]
fn test_propose_competition_same_miners_fails() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register and activate an issue
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Try to propose same miner twice
    let result = contract.propose_competition(1, accounts.bob, accounts.bob);
    assert_eq!(result, Err(Error::SameMiners));
}

#[ink::test]
fn test_propose_competition_issue_not_active() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register but don't fill issue (stays Registered)
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let result = contract.propose_competition(1, accounts.bob, accounts.charlie);
    assert_eq!(result, Err(Error::IssueNotActive));
}

#[ink::test]
fn test_propose_competition_miner_already_in_competition() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register and fill two issues
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/1"),
            String::from("test/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();
    contract
        .register_issue(
            String::from("https://github.com/test/repo/issues/2"),
            String::from("test/repo"),
            2,
            MIN_BOUNTY,
        )
        .unwrap();
    contract.alpha_pool = MIN_BOUNTY * 2;
    contract.fill_bounties();

    // Start competition with bob and charlie
    contract.start_competition(1, accounts.bob, accounts.charlie);

    // Try to propose bob for another competition
    let result = contract.propose_competition(2, accounts.bob, accounts.eve);
    assert_eq!(result, Err(Error::MinerAlreadyInCompetition));
}

// ================================================================
// Config Tests
// ================================================================

#[ink::test]
fn test_set_competition_config() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Verify defaults
    assert_eq!(contract.get_submission_window_blocks(), DEFAULT_SUBMISSION_WINDOW_BLOCKS);
    assert_eq!(contract.get_competition_deadline_blocks(), DEFAULT_COMPETITION_DEADLINE_BLOCKS);
    assert_eq!(contract.get_proposal_expiry_blocks(), DEFAULT_PROPOSAL_EXPIRY_BLOCKS);

    // Update config
    let result = contract.set_competition_config(100, 200, 50);
    assert!(result.is_ok());

    assert_eq!(contract.get_submission_window_blocks(), 100);
    assert_eq!(contract.get_competition_deadline_blocks(), 200);
    assert_eq!(contract.get_proposal_expiry_blocks(), 50);
}

#[ink::test]
fn test_set_competition_config_not_owner() {
    let accounts = default_accounts();
    set_caller(accounts.bob); // Not owner
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.set_competition_config(100, 200, 50);
    assert_eq!(result, Err(Error::NotOwner));
}

#[ink::test]
fn test_set_treasury_hotkey() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    assert_eq!(contract.treasury_hotkey(), accounts.bob);

    let result = contract.set_treasury_hotkey(accounts.charlie);
    assert!(result.is_ok());
    assert_eq!(contract.treasury_hotkey(), accounts.charlie);
}

// ================================================================
// Missing Error Variant Coverage
// ================================================================

#[ink::test]
fn test_cancel_issue_not_found() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.cancel_issue(999);
    assert_eq!(result, Err(Error::IssueNotFound));
}

#[ink::test]
fn test_register_issue_duplicate_url() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let url = String::from("https://github.com/test/repo/issues/1");
    contract.register_issue(url.clone(), String::from("test/repo"), 1, MIN_BOUNTY).unwrap();

    let result = contract.register_issue(url, String::from("test/repo"), 2, MIN_BOUNTY);
    assert_eq!(result, Err(Error::IssueAlreadyExists));
}

#[ink::test]
fn test_register_issue_zero_issue_number() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.register_issue(
        String::from("https://github.com/test/repo/issues/0"),
        String::from("test/repo"),
        0,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(Error::InvalidIssueNumber));
}

#[ink::test]
fn test_cancel_issue_in_competition() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    contract.start_competition(1, accounts.bob, accounts.charlie);

    let result = contract.cancel_issue(1);
    assert_eq!(result, Err(Error::CannotCancel));
}

#[ink::test]
fn test_cancel_issue_already_cancelled() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.cancel_issue(1).unwrap();

    let result = contract.cancel_issue(1);
    assert_eq!(result, Err(Error::CannotCancel));
}

// NOTE: This test is ignored because complete_competition uses call_runtime
// for auto-payout, which is not supported in off-chain tests.
#[ink::test]
#[ignore = "complete_competition uses call_runtime for auto-payout"]
fn test_validate_active_competition_not_active() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Set up and complete a competition
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);
    contract.complete_competition(comp_id, accounts.bob, [1u8; 32], accounts.bob);

    let result = contract.validate_active_competition(comp_id);
    assert_eq!(result, Err(Error::CompetitionNotActive));
}

#[ink::test]
fn test_propose_competition_issue_not_found() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Propose pair for non-existent issue
    let result = contract.propose_competition(1, accounts.bob, accounts.charlie);
    assert_eq!(result, Err(Error::IssueNotFound));
}

// NOTE: This test is ignored because propose_competition uses chain extensions
// for validator stake lookup, which is not supported in off-chain tests.
#[ink::test]
#[ignore = "propose_competition uses chain extensions for stake lookup"]
fn test_propose_competition_replaces_existing_proposal() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register and fill an issue
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Manually create an existing pair proposal
    let proposal = CompetitionProposal {
        issue_id: 1,
        miner1_hotkey: accounts.bob,
        miner2_hotkey: accounts.charlie,
        proposer: accounts.alice,
        proposed_at_block: 0,
        total_stake_voted: 100,
        votes_count: 1,
    };
    contract.competition_proposals.insert(1, &proposal);

    // New propose_competition should replace the existing proposal
    let result = contract.propose_competition(1, accounts.django, accounts.eve);

    // With REQUIRED_VALIDATOR_VOTES=1 and off-chain test (stake=0), this should fail
    // because the caller has no stake in off-chain tests
    assert_eq!(result, Err(Error::InsufficientStake));
}

// ================================================================
// Payout Bounty Validation
// ================================================================

#[ink::test]
fn test_payout_bounty_not_owner() {
    let accounts = default_accounts();
    set_caller(accounts.bob); // Not owner
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.payout_bounty(1, accounts.charlie);
    assert_eq!(result, Err(Error::NotOwner));
}

#[ink::test]
fn test_payout_bounty_competition_not_found() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.payout_bounty(999, accounts.charlie);
    assert_eq!(result, Err(Error::CompetitionNotFound));
}

#[ink::test]
fn test_payout_bounty_not_completed() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create an active competition
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);

    let result = contract.payout_bounty(comp_id, accounts.bob);
    assert_eq!(result, Err(Error::BountyNotCompleted));
}

// NOTE: This test is ignored because complete_competition uses call_runtime
// for auto-payout, which is not supported in off-chain tests.
#[ink::test]
#[ignore = "complete_competition uses call_runtime for auto-payout"]
fn test_payout_bounty_zero_amount() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create a completed competition with zero payout
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    let comp_id = contract.start_competition(1, accounts.bob, accounts.charlie);
    contract.complete_competition(comp_id, accounts.bob, [1u8; 32], accounts.bob);

    // Manually set payout_amount to 0 (complete_competition sets it to bounty_amount,
    // but complete_competition zeros issue.bounty_amount so payout is captured)
    // We need to override the stored competition
    let mut comp = contract.get_competition(comp_id).unwrap();
    comp.payout_amount = 0;
    contract.competitions.insert(comp_id, &comp);

    let result = contract.payout_bounty(comp_id, accounts.bob);
    assert_eq!(result, Err(Error::BountyNotFunded));
}

// ================================================================
// Edge Cases - Fill Bounties
// ================================================================

#[ink::test]
fn test_fill_bounties_empty_queue_with_funds() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Pool has funds but no issues in queue
    contract.alpha_pool = MIN_BOUNTY * 5;
    contract.fill_bounties();

    // Pool should remain unchanged
    assert_eq!(contract.get_alpha_pool(), MIN_BOUNTY * 5);
    assert!(contract.get_bounty_queue().is_empty());
}

#[ink::test]
fn test_fill_bounties_empty_pool_with_queue() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register issues but pool is empty
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();

    contract.fill_bounties();

    // Issue should remain Registered with no bounty
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.bounty_amount, 0);
    assert_eq!(issue.status, IssueStatus::Registered);
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn test_fill_bounties_cancelled_issue_in_queue() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register two issues
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/2"),
        String::from("test/repo"),
        2,
        MIN_BOUNTY,
    ).unwrap();

    // Cancel first issue
    contract.cancel_issue(1).unwrap();

    // Fill with enough for one issue
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Cancelled issue should be removed from queue, second issue filled
    let issue2 = contract.get_issue(2).unwrap();
    assert_eq!(issue2.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue2.status, IssueStatus::Active);
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn test_fill_bounties_multiple_partial_fills() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Register 3 issues
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/2"),
        String::from("test/repo"),
        2,
        MIN_BOUNTY,
    ).unwrap();
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/3"),
        String::from("test/repo"),
        3,
        MIN_BOUNTY,
    ).unwrap();

    // Add enough for 1.5 issues (FIFO: first fully filled, second partially)
    contract.alpha_pool = MIN_BOUNTY + MIN_BOUNTY / 2;
    contract.fill_bounties();

    // First issue fully funded
    let issue1 = contract.get_issue(1).unwrap();
    assert_eq!(issue1.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue1.status, IssueStatus::Active);

    // swap_remove reorders queue: after issue 1 removed, queue is [3, 2]
    // so issue 3 gets the partial fill next (FIFO with swap_remove behavior)
    let issue3 = contract.get_issue(3).unwrap();
    assert_eq!(issue3.bounty_amount, MIN_BOUNTY / 2);
    assert_eq!(issue3.status, IssueStatus::Registered);

    // Issue 2 unfunded (was swapped to back of queue)
    let issue2 = contract.get_issue(2).unwrap();
    assert_eq!(issue2.bounty_amount, 0);
    assert_eq!(issue2.status, IssueStatus::Registered);

    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn test_cancel_issue_with_bounty_returns_to_pool() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();
    assert_eq!(contract.get_alpha_pool(), 0);

    // Cancel the active issue — bounty should return to pool
    contract.cancel_issue(1).unwrap();

    assert_eq!(contract.get_alpha_pool(), MIN_BOUNTY);
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.status, IssueStatus::Cancelled);
    assert_eq!(issue.bounty_amount, 0);
}

#[ink::test]
fn test_cancel_issue_with_zero_bounty() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();

    // Cancel before any bounty is allocated
    contract.cancel_issue(1).unwrap();

    assert_eq!(contract.get_alpha_pool(), 0);
    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.status, IssueStatus::Cancelled);
    assert_eq!(issue.bounty_amount, 0);
}

#[ink::test]
fn test_register_multiple_issues_sequential_ids() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let id1 = contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    let id2 = contract.register_issue(
        String::from("https://github.com/test/repo/issues/2"),
        String::from("test/repo"),
        2,
        MIN_BOUNTY,
    ).unwrap();
    let id3 = contract.register_issue(
        String::from("https://github.com/test/repo/issues/3"),
        String::from("test/repo"),
        3,
        MIN_BOUNTY,
    ).unwrap();

    assert_eq!(id1, 1);
    assert_eq!(id2, 2);
    assert_eq!(id3, 3);
    assert_eq!(contract.next_issue_id(), 4);
}

#[ink::test]
fn test_bounty_queue_ordering_after_fill() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    contract.register_issue(
        String::from("https://github.com/test/repo/issues/1"),
        String::from("test/repo"),
        1,
        MIN_BOUNTY,
    ).unwrap();
    contract.register_issue(
        String::from("https://github.com/test/repo/issues/2"),
        String::from("test/repo"),
        2,
        MIN_BOUNTY,
    ).unwrap();

    // Fill only first issue
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Queue should only contain the remaining issue
    let queue = contract.get_bounty_queue();
    assert_eq!(queue.len(), 1);
    assert_eq!(queue[0], 2);
}

// ================================================================
// Vote Helper Coverage
// ================================================================

#[ink::test]
fn test_check_not_voted_timeout() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Initially not voted
    assert!(contract.check_not_voted_timeout(1, accounts.bob).is_ok());

    // Mark as voted
    contract.timeout_vote_voters.insert((1, accounts.bob), &true);

    // Should return AlreadyVoted
    assert_eq!(contract.check_not_voted_timeout(1, accounts.bob), Err(Error::AlreadyVoted));

    // Different user still ok
    assert!(contract.check_not_voted_timeout(1, accounts.charlie).is_ok());
}

#[ink::test]
fn test_check_not_voted_cancel_issue() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Initially not voted
    assert!(contract.check_not_voted_cancel_issue(1, accounts.bob).is_ok());

    // Mark as voted
    contract.cancel_issue_voters.insert((1, accounts.bob), &true);

    // Should return AlreadyVoted
    assert_eq!(contract.check_not_voted_cancel_issue(1, accounts.bob), Err(Error::AlreadyVoted));

    // Different user still ok
    assert!(contract.check_not_voted_cancel_issue(1, accounts.charlie).is_ok());
}

#[ink::test]
fn test_get_or_create_timeout_vote() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create new timeout vote
    let vote = contract.get_or_create_timeout_vote(1);
    assert_eq!(vote.competition_id, 1);
    assert_eq!(vote.reason_hash, [0u8; 32]);
    assert_eq!(vote.total_stake_voted, 0);
    assert_eq!(vote.votes_count, 0);

    // Store with data, then retrieve existing
    let mut stored_vote = vote;
    stored_vote.total_stake_voted = 500;
    stored_vote.votes_count = 3;
    contract.timeout_votes.insert(1, &stored_vote);

    let existing = contract.get_or_create_timeout_vote(1);
    assert_eq!(existing.total_stake_voted, 500);
    assert_eq!(existing.votes_count, 3);
}

#[ink::test]
fn test_get_or_create_cancel_issue_vote() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let reason = [5u8; 32];

    // Create new cancel vote for issue
    let vote = contract.get_or_create_cancel_issue_vote(1, reason);
    assert_eq!(vote.competition_id, 1); // Reused for issue_id
    assert_eq!(vote.reason_hash, reason);
    assert_eq!(vote.total_stake_voted, 0);
    assert_eq!(vote.votes_count, 0);

    // Store with data, then retrieve existing
    let mut stored_vote = vote;
    stored_vote.total_stake_voted = 1000;
    stored_vote.votes_count = 7;
    contract.cancel_issue_votes.insert(1, &stored_vote);

    // When existing vote exists, params are ignored
    let existing = contract.get_or_create_cancel_issue_vote(1, [9u8; 32]);
    assert_eq!(existing.total_stake_voted, 1000);
    assert_eq!(existing.votes_count, 7);
    assert_eq!(existing.reason_hash, reason); // Original reason preserved
}

#[ink::test]
fn test_clear_timeout_vote() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create a timeout vote
    let vote = CancelVote {
        competition_id: 1,
        reason_hash: [0u8; 32],
        total_stake_voted: 500,
        votes_count: 2,
    };
    contract.timeout_votes.insert(1, &vote);

    // Clear it
    contract.clear_timeout_vote(1);

    assert!(contract.timeout_votes.get(1).is_none());
}

#[ink::test]
fn test_clear_cancel_issue_vote() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    // Create a cancel vote for issue
    let vote = CancelVote {
        competition_id: 1, // Reused for issue_id
        reason_hash: [3u8; 32],
        total_stake_voted: 800,
        votes_count: 4,
    };
    contract.cancel_issue_votes.insert(1, &vote);

    // Clear it
    contract.clear_cancel_issue_vote(1);

    assert!(contract.cancel_issue_votes.get(1).is_none());
}

// ================================================================
// Admin Edge Cases
// ================================================================

#[ink::test]
fn test_set_owner_not_owner() {
    let accounts = default_accounts();
    set_caller(accounts.bob); // Not owner
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.set_owner(accounts.bob);
    assert_eq!(result, Err(Error::NotOwner));
}

#[ink::test]
fn test_set_treasury_hotkey_not_owner() {
    let accounts = default_accounts();
    set_caller(accounts.bob); // Not owner
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.set_treasury_hotkey(accounts.charlie);
    assert_eq!(result, Err(Error::NotOwner));
}

#[ink::test]
fn test_set_validator_hotkey_not_owner() {
    let accounts = default_accounts();
    set_caller(accounts.bob); // Not owner
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    let result = contract.set_validator_hotkey(accounts.charlie);
    assert_eq!(result, Err(Error::NotOwner));
}

// ================================================================
// Validator Hotkey & Constructor
// ================================================================

#[ink::test]
fn test_constructor_validator_hotkey() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    assert_eq!(contract.validator_hotkey(), accounts.charlie);
}

#[ink::test]
fn test_set_validator_hotkey() {
    let accounts = default_accounts();
    set_caller(accounts.alice);
    let mut contract = IssueBountyManager::new(accounts.alice, accounts.bob, accounts.charlie, 74);

    assert_eq!(contract.validator_hotkey(), accounts.charlie);

    let result = contract.set_validator_hotkey(accounts.django);
    assert!(result.is_ok());
    assert_eq!(contract.validator_hotkey(), accounts.django);
}
