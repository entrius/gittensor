use super::*;
use ink::env::test;
use scale::Encode;

/// Default netuid used across tests
const TEST_NETUID: u16 = 1;

/// Default stake amount returned by the mock chain extension (100 ALPHA)
const MOCK_STAKE: u64 = 100_000_000_000;

/// Creates distinct AccountIds for testing.
/// Each account is a 32-byte array with the given byte repeated.
fn account(byte: u8) -> AccountId {
    AccountId::from([byte; 32])
}

/// Standard set of test accounts. Use these by convention:
///   - account(1) = owner
///   - account(2) = treasury_hotkey
///   - account(3) = validator_hotkey
///   - account(4) = random non-owner caller
///   - account(5) = solver_coldkey
///   - account(6) = solver_hotkey
///
/// Creates a contract with sensible defaults.
///
/// Caller is set to account(1) (the owner) before construction.
fn create_default_contract() -> IssueBountyManager {
    // Set the caller for the constructor
    test::set_caller::<crate::CustomEnvironment>(account(1));

    IssueBountyManager::new(
        account(1), // owner
        account(2), // treasury_hotkey
        TEST_NETUID,
    )
}

/// Sets the caller for the next contract call.
fn set_caller(caller: AccountId) {
    test::set_caller::<crate::CustomEnvironment>(caller);
}

// ============================================================================
// Mock Chain Extension
// ============================================================================

/// Mock for Subtensor chain extension (extension 5001).
/// Intercepts get_stake_info (func 0) and proxy_call (func 16).
struct MockSubtensorExtension {
    stake_amount: u64,
}

impl ink::env::test::ChainExtension for MockSubtensorExtension {
    fn ext_id(&self) -> u16 {
        5001
    }

    /// Handles chain extension calls:
    ///   func 0 (get_stake_info) -> returns Some(StakeInfo) with self.stake_amount
    ///   func 16 (proxy_call) -> returns 0 (success)
    fn call(&mut self, func_id: u16, _input: &[u8], output: &mut Vec<u8>) -> u32 {
        match func_id {
            0 => {
                // Build a StakeInfo with the configured stake amount.
                // All other fields are zeroed/defaults -- only stake matters for tests.
                let stake_info = crate::StakeInfo {
                    hotkey: AccountId::from([0u8; 32]),
                    coldkey: AccountId::from([0u8; 32]),
                    netuid: scale::Compact(TEST_NETUID),
                    stake: scale::Compact(self.stake_amount),
                    locked: scale::Compact(0u64),
                    emission: scale::Compact(0u64),
                    tao_emission: scale::Compact(0u64),
                    drain: scale::Compact(0u64),
                    is_registered: true,
                };
                // Encode as Option<StakeInfo> = Some(stake_info)
                let result: Option<crate::StakeInfo> = Some(stake_info);
                result.encode_to(output);
                0 // success
            }
            16 => {
                // proxy_call -> return 0u32 (success)
                0u32.encode_to(output);
                0
            }
            _ => 1, // unknown function
        }
    }
}

/// Registers the mock chain extension so tests can call functions
/// that depend on get_stake_info (voting, treasury queries).
fn register_mock_extension() {
    register_mock_extension_with_stake(MOCK_STAKE);
}

/// Registers mock chain extension with a custom stake amount.
fn register_mock_extension_with_stake(stake: u64) {
    ink::env::test::register_chain_extension(MockSubtensorExtension {
        stake_amount: stake,
    });
}

#[ink::test]
fn constructor_sets_fields_correctly() {
    let contract = create_default_contract();

    assert_eq!(contract.owner(), account(1));
    assert_eq!(contract.treasury_hotkey(), account(2));
    assert_eq!(contract.netuid(), TEST_NETUID);
    assert_eq!(contract.next_issue_id(), 1);
    assert_eq!(contract.get_alpha_pool(), 0);
    assert_eq!(contract.get_bounty_queue(), Vec::<u64>::new());
    assert_eq!(contract.get_last_harvest_block(), 0);
}

#[ink::test]
fn get_issue_returns_none_for_nonexistent() {
    let contract = create_default_contract();
    assert_eq!(contract.get_issue(1), None);
    assert_eq!(contract.get_issue(999), None);
}

#[ink::test]
fn get_issue_by_url_hash_returns_zero_for_unknown() {
    let contract = create_default_contract();
    let unknown_hash = [0xAA; 32];
    assert_eq!(contract.get_issue_by_url_hash(unknown_hash), 0);
}

#[ink::test]
fn get_issues_by_status_returns_empty_initially() {
    let contract = create_default_contract();
    assert!(contract
        .get_issues_by_status(crate::IssueStatus::Registered)
        .is_empty());
    assert!(contract
        .get_issues_by_status(crate::IssueStatus::Active)
        .is_empty());
    assert!(contract
        .get_issues_by_status(crate::IssueStatus::Completed)
        .is_empty());
    assert!(contract
        .get_issues_by_status(crate::IssueStatus::Cancelled)
        .is_empty());
}

#[ink::test]
fn get_config_returns_correct_values() {
    let contract = create_default_contract();
    let config = contract.get_config();
    assert_eq!(config.required_validator_votes, REQUIRED_VALIDATOR_VOTES);
    assert_eq!(config.netuid, TEST_NETUID);
}

#[ink::test]
fn get_bounty_queue_returns_empty_initially() {
    let contract = create_default_contract();
    assert!(contract.get_bounty_queue().is_empty());
}

// ============================================================================
// Admin Setter Tests
// ============================================================================

#[ink::test]
fn set_owner_works_for_owner() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    assert!(contract.set_owner(account(4)).is_ok());
    assert_eq!(contract.owner(), account(4));
}

#[ink::test]
fn set_owner_fails_for_non_owner() {
    let mut contract = create_default_contract();
    set_caller(account(4)); // not the owner
    assert_eq!(contract.set_owner(account(4)), Err(crate::Error::NotOwner));
}

#[ink::test]
fn set_treasury_hotkey_works_for_owner() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    assert!(contract.set_treasury_hotkey(account(7)).is_ok());
    assert_eq!(contract.treasury_hotkey(), account(7));
}

#[ink::test]
fn set_treasury_hotkey_fails_for_non_owner() {
    let mut contract = create_default_contract();
    set_caller(account(4));
    assert_eq!(
        contract.set_treasury_hotkey(account(7)),
        Err(crate::Error::NotOwner),
    );
}

// ============================================================================
// Internal Helper Tests
// ============================================================================

#[ink::test]
fn is_valid_repo_name_accepts_valid_names() {
    let contract = create_default_contract();
    assert!(contract.is_valid_repo_name("owner/repo"));
    assert!(contract.is_valid_repo_name("a/b"));
    assert!(contract.is_valid_repo_name("my-org/my-repo"));
    assert!(contract.is_valid_repo_name("foo/bar.baz"));
}

#[ink::test]
fn is_valid_repo_name_rejects_invalid_names() {
    let contract = create_default_contract();
    assert!(!contract.is_valid_repo_name(""));
    assert!(!contract.is_valid_repo_name("noslash"));
    assert!(!contract.is_valid_repo_name("/leading"));
    assert!(!contract.is_valid_repo_name("trailing/"));
    assert!(!contract.is_valid_repo_name("two/slashes/here"));
}

#[ink::test]
fn is_modifiable_returns_true_for_registered_and_active() {
    let contract = create_default_contract();
    assert!(contract.is_modifiable(crate::IssueStatus::Registered));
    assert!(contract.is_modifiable(crate::IssueStatus::Active));
}

#[ink::test]
fn is_modifiable_returns_false_for_finalized() {
    let contract = create_default_contract();
    assert!(!contract.is_modifiable(crate::IssueStatus::Completed));
    assert!(!contract.is_modifiable(crate::IssueStatus::Cancelled));
}

#[ink::test]
fn check_consensus_with_required_votes() {
    let contract = create_default_contract();
    // REQUIRED_VALIDATOR_VOTES is currently 1
    assert!(!contract.check_consensus(0));
    assert!(contract.check_consensus(1));
    assert!(contract.check_consensus(5));
}

#[ink::test]
fn hash_string_is_deterministic() {
    let contract = create_default_contract();
    let hash1 = contract.hash_string("https://github.com/org/repo/issues/1");
    let hash2 = contract.hash_string("https://github.com/org/repo/issues/1");
    assert_eq!(hash1, hash2);
}

#[ink::test]
fn hash_string_differs_for_different_inputs() {
    let contract = create_default_contract();
    let hash1 = contract.hash_string("https://github.com/org/repo/issues/1");
    let hash2 = contract.hash_string("https://github.com/org/repo/issues/2");
    assert_ne!(hash1, hash2);
}

// ============================================================================
// Register Issue Tests
// ============================================================================

/// Helper: registers a standard test issue as the owner.
/// Returns the issue_id on success.
fn register_test_issue(contract: &mut IssueBountyManager) -> u64 {
    set_caller(account(1));
    contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .expect("register_issue should succeed")
}

#[ink::test]
fn register_issue_succeeds_with_valid_inputs() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    assert_eq!(id, 1);
    assert_eq!(contract.next_issue_id(), 2);

    // Issue should be stored and retrievable
    let issue = contract.get_issue(id).expect("issue should exist");
    assert_eq!(issue.id, 1);
    assert_eq!(issue.repository_full_name, "org/repo");
    assert_eq!(issue.issue_number, 1);
    assert_eq!(issue.target_bounty, MIN_BOUNTY);
    assert_eq!(issue.bounty_amount, 0);
    assert_eq!(issue.status, crate::IssueStatus::Registered);
    assert_eq!(issue.solver_coldkey, None);
}

#[ink::test]
fn register_issue_adds_to_bounty_queue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);
    assert_eq!(contract.get_bounty_queue(), vec![id]);
}

#[ink::test]
fn register_issue_is_findable_by_url_hash() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Compute the same hash the contract would
    let url_hash = contract.hash_string("https://github.com/org/repo/issues/1");
    assert_eq!(contract.get_issue_by_url_hash(url_hash), id);
}

#[ink::test]
fn register_issue_appears_in_status_query() {
    let mut contract = create_default_contract();
    register_test_issue(&mut contract);

    let registered = contract.get_issues_by_status(crate::IssueStatus::Registered);
    assert_eq!(registered.len(), 1);
    assert_eq!(registered[0].issue_number, 1);

    // Other statuses should still be empty
    assert!(contract
        .get_issues_by_status(crate::IssueStatus::Active)
        .is_empty());
}

#[ink::test]
fn register_issue_increments_id_for_multiple_issues() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    let id1 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let id2 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY * 2,
        )
        .unwrap();

    assert_eq!(id1, 1);
    assert_eq!(id2, 2);
    assert_eq!(contract.next_issue_id(), 3);
    assert_eq!(contract.get_bounty_queue(), vec![1, 2]);
}

#[ink::test]
fn register_issue_fails_for_non_owner() {
    let mut contract = create_default_contract();
    set_caller(account(4)); // not the owner
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        1,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(crate::Error::NotOwner));
}

#[ink::test]
fn register_issue_fails_bounty_too_low() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        1,
        MIN_BOUNTY - 1, // one below minimum
    );
    assert_eq!(result, Err(crate::Error::BountyTooLow));
}

#[ink::test]
fn register_issue_fails_bounty_zero() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        1,
        0,
    );
    assert_eq!(result, Err(crate::Error::BountyTooLow));
}

#[ink::test]
fn register_issue_fails_issue_number_zero() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        0, // invalid
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(crate::Error::InvalidIssueNumber));
}

#[ink::test]
fn register_issue_fails_invalid_repo_name() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    // No slash
    let result = contract.register_issue(
        String::from("https://github.com/bad"),
        String::from("noslash"),
        1,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(crate::Error::InvalidRepositoryName));
}

#[ink::test]
fn register_issue_fails_duplicate_url() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    // First registration succeeds
    contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    // Same URL again fails
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        1,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(crate::Error::IssueAlreadyExists));
}

#[ink::test]
fn register_issue_at_exact_min_bounty_succeeds() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        1,
        MIN_BOUNTY, // exactly at the boundary
    );
    assert!(result.is_ok());
}

// ============================================================================
// Cancel Issue Tests
// ============================================================================

#[ink::test]
fn cancel_issue_succeeds_on_registered_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(1));
    assert!(contract.cancel_issue(id).is_ok());

    let issue = contract.get_issue(id).expect("issue should still exist");
    assert_eq!(issue.status, crate::IssueStatus::Cancelled);
    assert_eq!(issue.bounty_amount, 0);
}

#[ink::test]
fn cancel_issue_removes_from_bounty_queue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);
    assert_eq!(contract.get_bounty_queue(), vec![id]);

    set_caller(account(1));
    contract.cancel_issue(id).unwrap();
    assert!(contract.get_bounty_queue().is_empty());
}

#[ink::test]
fn cancel_issue_returns_bounty_to_alpha_pool() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Manually give the issue some bounty to test the return path.
    // We access the storage directly since fill_bounties needs chain ext.
    if let Some(mut issue) = contract.issues.get(id) {
        issue.bounty_amount = 5_000_000_000; // 5 ALPHA
        contract.issues.insert(id, &issue);
    }

    assert_eq!(contract.get_alpha_pool(), 0);
    set_caller(account(1));
    contract.cancel_issue(id).unwrap();

    // Bounty should have been returned to the pool
    assert_eq!(contract.get_alpha_pool(), 5_000_000_000);
}

#[ink::test]
fn cancel_issue_fails_for_non_owner() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(4));
    assert_eq!(contract.cancel_issue(id), Err(crate::Error::NotOwner));
}

#[ink::test]
fn cancel_issue_fails_for_nonexistent_issue() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    assert_eq!(contract.cancel_issue(999), Err(crate::Error::IssueNotFound));
}

#[ink::test]
fn cancel_issue_fails_on_already_cancelled() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(1));
    contract.cancel_issue(id).unwrap();

    // Second cancel should fail -- status is now Cancelled, not modifiable
    let result = contract.cancel_issue(id);
    assert_eq!(result, Err(crate::Error::CannotCancel));
}

#[ink::test]
fn cancel_issue_shows_in_status_query() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(1));
    contract.cancel_issue(id).unwrap();

    assert!(contract
        .get_issues_by_status(crate::IssueStatus::Registered)
        .is_empty());
    let cancelled = contract.get_issues_by_status(crate::IssueStatus::Cancelled);
    assert_eq!(cancelled.len(), 1);
    assert_eq!(cancelled[0].id, id);
}

#[ink::test]
fn cancel_middle_issue_preserves_other_queue_entries() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    let id1 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let id2 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY,
        )
        .unwrap();

    let id3 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/3"),
            String::from("org/repo"),
            3,
            MIN_BOUNTY,
        )
        .unwrap();

    // Cancel the middle one
    contract.cancel_issue(id2).unwrap();

    // Queue should have id1 and id3 (swap_remove puts last in middle's spot)
    let queue = contract.get_bounty_queue();
    assert_eq!(queue.len(), 2);
    assert!(queue.contains(&id1));
    assert!(queue.contains(&id3));
    assert!(!queue.contains(&id2));
}

// ============================================================================
// Fill Bounties Tests
// ============================================================================

#[ink::test]
fn fill_bounties_allocates_from_alpha_pool() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Simulate available emissions by setting alpha_pool directly
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue.status, crate::IssueStatus::Active);
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn fill_bounties_partial_fill_stays_registered() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Only give half the needed bounty
    let half = MIN_BOUNTY / 2;
    contract.alpha_pool = half;
    contract.fill_bounties();

    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.bounty_amount, half);
    assert_eq!(issue.status, crate::IssueStatus::Registered); // not Active yet
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn fill_bounties_fills_fifo_order() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    // Register two issues, each needing MIN_BOUNTY
    let id1 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let id2 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY,
        )
        .unwrap();

    // Only enough to fill the first one
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    let issue1 = contract.get_issue(id1).unwrap();
    let issue2 = contract.get_issue(id2).unwrap();

    assert_eq!(issue1.status, crate::IssueStatus::Active);
    assert_eq!(issue1.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue2.status, crate::IssueStatus::Registered);
    assert_eq!(issue2.bounty_amount, 0);
}

#[ink::test]
fn fill_bounties_fills_multiple_when_pool_sufficient() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY,
        )
        .unwrap();

    // Enough for both plus some leftover
    contract.alpha_pool = MIN_BOUNTY * 3;
    contract.fill_bounties();

    let issue1 = contract.get_issue(1).unwrap();
    let issue2 = contract.get_issue(2).unwrap();
    assert_eq!(issue1.status, crate::IssueStatus::Active);
    assert_eq!(issue2.status, crate::IssueStatus::Active);
    assert_eq!(contract.get_alpha_pool(), MIN_BOUNTY); // leftover
}

#[ink::test]
fn fill_bounties_skips_cancelled_issues() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    let id1 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let id2 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY,
        )
        .unwrap();

    // Cancel the first issue
    contract.cancel_issue(id1).unwrap();

    // Give enough for one issue
    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // id2 should get funded, not the cancelled id1
    let issue2 = contract.get_issue(id2).unwrap();
    assert_eq!(issue2.status, crate::IssueStatus::Active);
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn fill_bounties_noop_when_pool_empty() {
    let mut contract = create_default_contract();
    register_test_issue(&mut contract);

    contract.alpha_pool = 0;
    contract.fill_bounties();

    let issue = contract.get_issue(1).unwrap();
    assert_eq!(issue.bounty_amount, 0);
    assert_eq!(issue.status, crate::IssueStatus::Registered);
}

// ============================================================================
// Get Total Committed Tests
// ============================================================================

#[ink::test]
fn get_total_committed_zero_initially() {
    let contract = create_default_contract();
    assert_eq!(contract.get_total_committed(), 0);
}

#[ink::test]
fn get_total_committed_sums_registered_bounties() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Give the issue some bounty
    if let Some(mut issue) = contract.issues.get(id) {
        issue.bounty_amount = 5_000_000_000;
        contract.issues.insert(id, &issue);
    }

    assert_eq!(contract.get_total_committed(), 5_000_000_000);
}

#[ink::test]
fn get_total_committed_ignores_cancelled() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(1));
    contract.cancel_issue(id).unwrap();

    assert_eq!(contract.get_total_committed(), 0);
}

#[ink::test]
fn payout_bounty_fails_on_non_completed_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);
    set_caller(account(1));
    let result = contract.payout_bounty(id);
    assert_eq!(result, Err(crate::Error::BountyNotCompleted));
}

#[ink::test]
fn payout_bounty_fails_for_nonexistent_issue() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let result = contract.payout_bounty(74);

    assert_eq!(result, Err(crate::Error::IssueNotFound));
}

#[ink::test]
fn payout_bounty_fails_for_non_owner() {
    let mut contract = create_default_contract();
    set_caller(account(74));
    let result = contract.payout_bounty(74);

    assert_eq!(result, Err(crate::Error::NotOwner));
}

#[ink::test]
fn payout_bounty_fails_when_already_paid() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let id = register_test_issue(&mut contract);

    if let Some(mut issue) = contract.issues.get(id) {
        issue.status = crate::IssueStatus::Completed;
        contract.issues.insert(id, &issue);
    }

    let result = contract.payout_bounty(id);

    assert_eq!(result, Err(crate::Error::BountyAlreadyPaid));
}

#[ink::test]
fn cancel_issue_fails_on_completed_issue() {
    let mut contract = create_default_contract();
    set_caller(account(1));
    let id = register_test_issue(&mut contract);

    if let Some(mut issue) = contract.issues.get(id) {
        issue.status = crate::IssueStatus::Completed;
        contract.issues.insert(id, &issue);
    }

    let result = contract.cancel_issue(id);

    assert_eq!(result, Err(crate::Error::CannotCancel));
}

// ============================================================================
// Payout Bounty Tests (additional)
// ============================================================================

#[ink::test]
fn payout_bounty_fails_no_solver_set() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Set to Completed with funds but no solver_coldkey
    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Completed;
    issue.bounty_amount = MIN_BOUNTY;
    // solver_coldkey is already None from registration
    contract.issues.insert(id, &issue);

    set_caller(account(1));
    let result = contract.payout_bounty(id);
    assert_eq!(result, Err(crate::Error::NoSolverSet));
}

// ============================================================================
// Vote Solution Tests (validation paths -- chain extension blocks full flow)
// ============================================================================

#[ink::test]
fn vote_solution_fails_issue_not_found() {
    let mut contract = create_default_contract();
    set_caller(account(4));
    let result = contract.vote_solution(
        999,
        account(6), // solver_hotkey
        account(5), // solver_coldkey
        42,         // pr_number
    );
    assert_eq!(result, Err(crate::Error::IssueNotFound));
}

#[ink::test]
fn vote_solution_fails_issue_not_active() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Issue is Registered, not Active
    set_caller(account(4));
    let result = contract.vote_solution(id, account(6), account(5), 42);
    assert_eq!(result, Err(crate::Error::IssueNotActive));
}

#[ink::test]
fn vote_solution_fails_on_completed_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Completed;
    contract.issues.insert(id, &issue);

    set_caller(account(4));
    let result = contract.vote_solution(id, account(6), account(5), 42);
    assert_eq!(result, Err(crate::Error::IssueNotActive));
}

#[ink::test]
fn vote_solution_fails_on_cancelled_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(1));
    contract.cancel_issue(id).unwrap();

    set_caller(account(4));
    let result = contract.vote_solution(id, account(6), account(5), 42);
    assert_eq!(result, Err(crate::Error::IssueNotActive));
}

#[ink::test]
fn vote_solution_fails_already_voted() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Make issue Active
    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Active;
    issue.bounty_amount = MIN_BOUNTY;
    contract.issues.insert(id, &issue);

    // Manually mark account(4) as having voted
    contract
        .solution_vote_voters
        .insert((id, account(4)), &true);

    set_caller(account(4));
    let result = contract.vote_solution(id, account(6), account(5), 42);
    assert_eq!(result, Err(crate::Error::AlreadyVoted));
}

// ============================================================================
// Vote Cancel Issue Tests (validation paths)
// ============================================================================

#[ink::test]
fn vote_cancel_issue_fails_issue_not_found() {
    let mut contract = create_default_contract();
    set_caller(account(4));
    let result = contract.vote_cancel_issue(999, [0xCC; 32]);
    assert_eq!(result, Err(crate::Error::IssueNotFound));
}

#[ink::test]
fn vote_cancel_issue_fails_on_completed_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Completed;
    contract.issues.insert(id, &issue);

    set_caller(account(4));
    let result = contract.vote_cancel_issue(id, [0xCC; 32]);
    assert_eq!(result, Err(crate::Error::IssueAlreadyFinalized));
}

#[ink::test]
fn vote_cancel_issue_fails_on_cancelled_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(1));
    contract.cancel_issue(id).unwrap();

    set_caller(account(4));
    let result = contract.vote_cancel_issue(id, [0xCC; 32]);
    assert_eq!(result, Err(crate::Error::IssueAlreadyFinalized));
}

#[ink::test]
fn vote_cancel_issue_fails_already_voted() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Manually mark account(4) as having voted to cancel
    contract.cancel_issue_voters.insert((id, account(4)), &true);

    set_caller(account(4));
    let result = contract.vote_cancel_issue(id, [0xCC; 32]);
    assert_eq!(result, Err(crate::Error::AlreadyVoted));
}

// ============================================================================
// Queue Helper Tests (Order-Preserving Removal)
// ============================================================================

#[ink::test]
fn remove_at_removes_only_element() {
    let mut contract = create_default_contract();
    contract.bounty_queue.push(1);

    contract.remove_at(0);
    assert!(contract.bounty_queue.is_empty());
}

#[ink::test]
fn remove_at_removes_last_element() {
    let mut contract = create_default_contract();
    contract.bounty_queue.push(1);
    contract.bounty_queue.push(2);
    contract.bounty_queue.push(3);

    contract.remove_at(2); // remove last
    assert_eq!(contract.bounty_queue, vec![1, 2]);
}

#[ink::test]
fn remove_at_preserves_order() {
    let mut contract = create_default_contract();
    contract.bounty_queue.push(1);
    contract.bounty_queue.push(2);
    contract.bounty_queue.push(3);

    contract.remove_at(0); // remove first, order preserved
    assert_eq!(contract.bounty_queue, vec![2, 3]);
}

#[ink::test]
fn remove_at_noop_on_empty() {
    let mut contract = create_default_contract();
    contract.remove_at(0); // should not panic
    assert!(contract.bounty_queue.is_empty());
}

#[ink::test]
fn remove_from_bounty_queue_noop_for_missing_id() {
    let mut contract = create_default_contract();
    contract.bounty_queue.push(1);
    contract.bounty_queue.push(2);

    contract.remove_from_bounty_queue(999); // not in queue
    assert_eq!(contract.bounty_queue, vec![1, 2]);
}

// ============================================================================
// Vote Record Helper Tests
// ============================================================================

#[ink::test]
fn get_or_create_solution_vote_creates_new() {
    let mut contract = create_default_contract();
    let vote = contract.get_or_create_solution_vote(1, account(6), 42, account(5));

    assert_eq!(vote.issue_id, 1);
    assert_eq!(vote.solver_hotkey, account(6));
    assert_eq!(vote.solver_coldkey, account(5));
    assert_eq!(vote.pr_number, 42);
    assert_eq!(vote.votes_count, 0);
    assert_eq!(vote.total_stake_voted, 0);
}

#[ink::test]
fn get_or_create_solution_vote_returns_existing() {
    let mut contract = create_default_contract();

    // Store an existing vote with some data
    let existing = crate::SolutionVote {
        issue_id: 1,
        solver_hotkey: account(6),
        solver_coldkey: account(5),
        pr_number: 42,
        total_stake_voted: 500,
        votes_count: 3,
    };
    contract.solution_votes.insert(1, &existing);

    let vote = contract.get_or_create_solution_vote(
        1,
        account(7), // different solver -- should be ignored
        99,         // different pr -- should be ignored
        account(8),
    );

    // Should return the stored vote, not create a new one
    assert_eq!(vote.solver_hotkey, account(6));
    assert_eq!(vote.votes_count, 3);
    assert_eq!(vote.total_stake_voted, 500);
}

#[ink::test]
fn get_or_create_cancel_issue_vote_creates_new() {
    let mut contract = create_default_contract();
    let vote = contract.get_or_create_cancel_issue_vote(1, [0xCC; 32]);

    assert_eq!(vote.issue_id, 1);
    assert_eq!(vote.reason_hash, [0xCC; 32]);
    assert_eq!(vote.votes_count, 0);
    assert_eq!(vote.total_stake_voted, 0);
}

#[ink::test]
fn get_or_create_cancel_issue_vote_returns_existing() {
    let mut contract = create_default_contract();

    let existing = crate::CancelVote {
        issue_id: 1,
        reason_hash: [0xCC; 32],
        total_stake_voted: 1000,
        votes_count: 2,
    };
    contract.cancel_issue_votes.insert(1, &existing);

    let vote = contract.get_or_create_cancel_issue_vote(
        1, [0xFF; 32], // different hash -- should be ignored
    );

    assert_eq!(vote.reason_hash, [0xCC; 32]);
    assert_eq!(vote.votes_count, 2);
    assert_eq!(vote.total_stake_voted, 1000);
}

// ============================================================================
// Clear Vote Tests
// ============================================================================

#[ink::test]
fn clear_solution_vote_removes_record() {
    let mut contract = create_default_contract();
    let vote = crate::SolutionVote {
        issue_id: 1,
        solver_hotkey: account(6),
        solver_coldkey: account(5),
        pr_number: 42,
        total_stake_voted: 100,
        votes_count: 1,
    };
    contract.solution_votes.insert(1, &vote);

    contract.clear_solution_vote(1);
    assert!(contract.solution_votes.get(1).is_none());
}

#[ink::test]
fn clear_cancel_issue_vote_removes_record() {
    let mut contract = create_default_contract();
    let vote = crate::CancelVote {
        issue_id: 1,
        reason_hash: [0xCC; 32],
        total_stake_voted: 100,
        votes_count: 1,
    };
    contract.cancel_issue_votes.insert(1, &vote);

    contract.clear_cancel_issue_vote(1);
    assert!(contract.cancel_issue_votes.get(1).is_none());
}

// ============================================================================
// Admin Setter Edge Cases
// ============================================================================

#[ink::test]
fn set_owner_transfers_authority() {
    let mut contract = create_default_contract();

    // Transfer ownership to account(4)
    set_caller(account(1));
    contract.set_owner(account(4)).unwrap();

    // Old owner can no longer act
    set_caller(account(1));
    assert_eq!(contract.set_owner(account(1)), Err(crate::Error::NotOwner));

    // New owner can act
    set_caller(account(4));
    assert!(contract.set_owner(account(4)).is_ok());
}

#[ink::test]
fn new_owner_can_register_issues() {
    let mut contract = create_default_contract();

    set_caller(account(1));
    contract.set_owner(account(4)).unwrap();

    // New owner registers an issue
    set_caller(account(4));
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/1"),
        String::from("org/repo"),
        1,
        MIN_BOUNTY,
    );
    assert!(result.is_ok());

    // Old owner cannot
    set_caller(account(1));
    let result = contract.register_issue(
        String::from("https://github.com/org/repo/issues/2"),
        String::from("org/repo"),
        2,
        MIN_BOUNTY,
    );
    assert_eq!(result, Err(crate::Error::NotOwner));
}

// ============================================================================
// Get Total Committed (additional)
// ============================================================================

#[ink::test]
fn get_total_committed_sums_multiple_issues() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    let id1 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    let id2 = contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY * 2,
        )
        .unwrap();

    // Give each issue partial bounty
    let mut issue1 = contract.issues.get(id1).unwrap();
    issue1.bounty_amount = 3_000_000_000;
    contract.issues.insert(id1, &issue1);

    let mut issue2 = contract.issues.get(id2).unwrap();
    issue2.bounty_amount = 7_000_000_000;
    contract.issues.insert(id2, &issue2);

    assert_eq!(contract.get_total_committed(), 10_000_000_000);
}

#[ink::test]
fn get_total_committed_includes_active_issues() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Fund it and make it Active
    let mut issue = contract.issues.get(id).unwrap();
    issue.bounty_amount = MIN_BOUNTY;
    issue.status = crate::IssueStatus::Active;
    contract.issues.insert(id, &issue);

    assert_eq!(contract.get_total_committed(), MIN_BOUNTY);
}

#[ink::test]
fn get_total_committed_ignores_completed() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    let mut issue = contract.issues.get(id).unwrap();
    issue.bounty_amount = MIN_BOUNTY;
    issue.status = crate::IssueStatus::Completed;
    contract.issues.insert(id, &issue);

    assert_eq!(contract.get_total_committed(), 0);
}

// ============================================================================
// Fill Bounties Edge Cases
// ============================================================================

#[ink::test]
fn fill_bounties_resumes_partial_fill() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // First fill: give half
    let half = MIN_BOUNTY / 2;
    contract.alpha_pool = half;
    contract.fill_bounties();

    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.bounty_amount, half);
    assert_eq!(issue.status, crate::IssueStatus::Registered);

    // Second fill: give the other half
    contract.alpha_pool = half;
    contract.fill_bounties();

    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue.status, crate::IssueStatus::Active);
}

#[ink::test]
fn fill_bounties_with_different_target_amounts() {
    let mut contract = create_default_contract();
    set_caller(account(1));

    // Small bounty
    contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/1"),
            String::from("org/repo"),
            1,
            MIN_BOUNTY,
        )
        .unwrap();

    // Large bounty (5x)
    contract
        .register_issue(
            String::from("https://github.com/org/repo/issues/2"),
            String::from("org/repo"),
            2,
            MIN_BOUNTY * 5,
        )
        .unwrap();

    // Give enough for the small one plus partial for the large one
    contract.alpha_pool = MIN_BOUNTY * 2;
    contract.fill_bounties();

    let issue1 = contract.get_issue(1).unwrap();
    let issue2 = contract.get_issue(2).unwrap();

    assert_eq!(issue1.status, crate::IssueStatus::Active);
    assert_eq!(issue1.bounty_amount, MIN_BOUNTY);
    assert_eq!(issue2.status, crate::IssueStatus::Registered);
    assert_eq!(issue2.bounty_amount, MIN_BOUNTY); // got the remainder
    assert_eq!(contract.get_alpha_pool(), 0);
}

#[ink::test]
fn fill_bounties_fully_funded_removed_from_queue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    contract.alpha_pool = MIN_BOUNTY;
    contract.fill_bounties();

    // Fully funded issue should be removed from the queue
    assert!(!contract.get_bounty_queue().contains(&id));
}

// ============================================================================
// Cancel Issue on Active Issue
// ============================================================================

#[ink::test]
fn cancel_issue_succeeds_on_active_issue() {
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    // Make it active
    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Active;
    issue.bounty_amount = MIN_BOUNTY;
    contract.issues.insert(id, &issue);

    set_caller(account(1));
    assert!(contract.cancel_issue(id).is_ok());

    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.status, crate::IssueStatus::Cancelled);
    assert_eq!(issue.bounty_amount, 0);
    assert_eq!(contract.get_alpha_pool(), MIN_BOUNTY);
}

// ============================================================================
// Chain Extension Mock Tests -- Treasury / Validator Stake Queries
// ============================================================================

#[ink::test]
fn get_treasury_stake_returns_mocked_value() {
    register_mock_extension();
    let contract = create_default_contract();
    let stake = contract.get_treasury_stake();
    assert_eq!(stake, MOCK_STAKE as u128);
}

#[ink::test]
fn get_treasury_stake_returns_zero_when_no_stake() {
    register_mock_extension_with_stake(0);
    let contract = create_default_contract();
    // Stake is 0 but Some(StakeInfo) is returned -- should get 0
    let stake = contract.get_treasury_stake();
    assert_eq!(stake, 0);
}

#[ink::test]
fn get_validator_stake_returns_mocked_value() {
    register_mock_extension();
    let contract = create_default_contract();
    let stake = contract.get_validator_stake(account(4));
    assert_eq!(stake, MOCK_STAKE as u128);
}

// ============================================================================
// Vote Solution Happy Path (with mocked chain extension)
// ============================================================================

/// Helper: creates a contract with an Active issue and mock extension.
/// bounty_amount is set to 0 so that complete_issue/execute_cancel_issue
/// won't trigger proxy_call (payout/recycle paths).
/// This lets us test the full consensus/completion/cancellation flow.
/// Payout transfers require E2E tests against a real Subtensor node.
fn setup_active_issue_with_mock() -> (IssueBountyManager, u64) {
    register_mock_extension();
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Active;
    issue.bounty_amount = 0; // zero avoids proxy_call in payout/recycle paths
    contract.issues.insert(id, &issue);

    (contract, id)
}

#[ink::test]
fn vote_solution_succeeds_and_completes_issue() {
    let (mut contract, id) = setup_active_issue_with_mock();

    // account(4) votes as a validator with mocked stake
    set_caller(account(4));
    let result = contract.vote_solution(
        id,
        account(6), // solver_hotkey
        account(5), // solver_coldkey
        42,         // pr_number
    );
    assert!(result.is_ok());

    // With REQUIRED_VALIDATOR_VOTES = 1, one vote should complete the issue
    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.status, crate::IssueStatus::Completed);
    assert_eq!(issue.solver_coldkey, Some(account(5)));
}

#[ink::test]
fn vote_solution_removes_issue_from_bounty_queue() {
    let (mut contract, id) = setup_active_issue_with_mock();
    // register_test_issue already added id to the queue

    assert!(contract.get_bounty_queue().contains(&id));

    set_caller(account(4));
    contract
        .vote_solution(id, account(6), account(5), 42)
        .unwrap();

    assert!(!contract.get_bounty_queue().contains(&id));
}

#[ink::test]
fn vote_solution_clears_vote_record_after_consensus() {
    let (mut contract, id) = setup_active_issue_with_mock();

    set_caller(account(4));
    contract
        .vote_solution(id, account(6), account(5), 42)
        .unwrap();

    // Vote record should be cleaned up after consensus
    assert!(contract.solution_votes.get(id).is_none());
}

#[ink::test]
fn vote_solution_records_voter() {
    let (mut contract, id) = setup_active_issue_with_mock();

    set_caller(account(4));
    contract
        .vote_solution(id, account(6), account(5), 42)
        .unwrap();

    // Voter should be recorded (prevents double voting)
    assert!(contract
        .solution_vote_voters
        .get((id, account(4)))
        .unwrap_or(false));
}

#[ink::test]
fn vote_solution_fails_with_zero_stake() {
    register_mock_extension_with_stake(0);
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    let mut issue = contract.issues.get(id).unwrap();
    issue.status = crate::IssueStatus::Active;
    issue.bounty_amount = MIN_BOUNTY;
    contract.issues.insert(id, &issue);

    set_caller(account(4));
    let result = contract.vote_solution(id, account(6), account(5), 42);
    assert_eq!(result, Err(crate::Error::InsufficientStake));
}

// ============================================================================
// Vote Cancel Issue Happy Path (with mocked chain extension)
// ============================================================================

#[ink::test]
fn vote_cancel_issue_succeeds_on_registered_issue() {
    register_mock_extension();
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(4));
    let result = contract.vote_cancel_issue(id, [0xCC; 32]);
    assert!(result.is_ok());

    // With REQUIRED_VALIDATOR_VOTES = 1, issue should be cancelled
    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.status, crate::IssueStatus::Cancelled);
    assert_eq!(issue.bounty_amount, 0);
}

#[ink::test]
fn vote_cancel_issue_succeeds_on_active_issue() {
    let (mut contract, id) = setup_active_issue_with_mock();
    // bounty_amount is 0 from setup, so recycle(0) returns true
    // without calling proxy_call

    set_caller(account(4));
    let result = contract.vote_cancel_issue(id, [0xCC; 32]);
    assert!(result.is_ok());

    let issue = contract.get_issue(id).unwrap();
    assert_eq!(issue.status, crate::IssueStatus::Cancelled);
}

#[ink::test]
fn vote_cancel_issue_removes_from_bounty_queue() {
    register_mock_extension();
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    assert!(contract.get_bounty_queue().contains(&id));

    set_caller(account(4));
    contract.vote_cancel_issue(id, [0xCC; 32]).unwrap();

    assert!(!contract.get_bounty_queue().contains(&id));
}

#[ink::test]
fn vote_cancel_issue_clears_vote_record_after_consensus() {
    register_mock_extension();
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(4));
    contract.vote_cancel_issue(id, [0xCC; 32]).unwrap();

    assert!(contract.cancel_issue_votes.get(id).is_none());
}

#[ink::test]
fn vote_cancel_issue_records_voter() {
    register_mock_extension();
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(4));
    contract.vote_cancel_issue(id, [0xCC; 32]).unwrap();

    assert!(contract
        .cancel_issue_voters
        .get((id, account(4)))
        .unwrap_or(false));
}

#[ink::test]
fn vote_cancel_issue_fails_with_zero_stake() {
    register_mock_extension_with_stake(0);
    let mut contract = create_default_contract();
    let id = register_test_issue(&mut contract);

    set_caller(account(4));
    let result = contract.vote_cancel_issue(id, [0xCC; 32]);
    assert_eq!(result, Err(crate::Error::InsufficientStake));
}
