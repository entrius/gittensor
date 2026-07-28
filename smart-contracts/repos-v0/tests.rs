use super::*;
use ink::env::test;
use scale::{Decode, Encode};

const TEST_NETUID: u16 = 1;

fn account(byte: u8) -> AccountId {
    AccountId::from([byte; 32])
}

/// Convention: account(1) = contract owner, account(2) = fee hotkey,
/// account(3..) = repo owners / voters.
fn set_caller(caller: AccountId) {
    test::set_caller::<crate::CustomEnvironment>(caller);
}

fn advance_blocks(n: u64) {
    for _ in 0..n {
        test::advance_block::<crate::CustomEnvironment>();
    }
}

// ============================================================================
// Mock Chain Extension (ext 5001: funcs 0 / 16 / 25)
// ============================================================================

struct MockExt {
    stake: u64,
    transfer_status: u32,
    /// Credited amount = min(requested, cap) — simulates a silent cap
    transfer_credit_cap: u64,
    recycle_status: u32,
    /// Returned recycled amount = requested - shortfall
    recycle_shortfall: u64,
}

impl Default for MockExt {
    fn default() -> Self {
        Self {
            stake: 0,
            transfer_status: 0,
            transfer_credit_cap: u64::MAX,
            recycle_status: 0,
            recycle_shortfall: 0,
        }
    }
}

impl ink::env::test::ChainExtension for MockExt {
    fn ext_id(&self) -> u16 {
        5001
    }

    fn call(&mut self, func_id: u16, input: &[u8], output: &mut Vec<u8>) -> u32 {
        // The off-chain engine passes args with a compact length prefix
        let mut args_buf = input;
        let _len: scale::Compact<u32> = Decode::decode(&mut args_buf).expect("input len");
        match func_id {
            0 => {
                let info = crate::StakeInfo {
                    hotkey: account(0),
                    coldkey: account(0),
                    netuid: scale::Compact(TEST_NETUID),
                    stake: scale::Compact(self.stake),
                    locked: scale::Compact(0u64),
                    emission: scale::Compact(0u64),
                    tao_emission: scale::Compact(0u64),
                    drain: scale::Compact(0u64),
                    is_registered: true,
                };
                Some(info).encode_to(output);
                0
            }
            25 => {
                if self.transfer_status == 0 {
                    let args: ([u8; 32], [u8; 32], u16, u16, u64) =
                        Decode::decode(&mut args_buf).expect("transfer args");
                    self.stake = self
                        .stake
                        .saturating_add(core::cmp::min(args.4, self.transfer_credit_cap));
                }
                self.transfer_status
            }
            16 => {
                if self.recycle_status == 0 {
                    let args: ([u8; 32], u16, u64) =
                        Decode::decode(&mut args_buf).expect("recycle args");
                    args.2.saturating_sub(self.recycle_shortfall).encode_to(output);
                }
                self.recycle_status
            }
            _ => 1,
        }
    }
}

fn register_mock() {
    test::register_chain_extension(MockExt::default());
}

// ============================================================================
// Setup helpers
// ============================================================================

fn create_contract() -> RepoRegistry {
    set_caller(account(1));
    RepoRegistry::new(account(1), TEST_NETUID)
}

fn small_constants() -> crate::Constants {
    crate::Constants {
        max_repos: 3,
        immunity_period: 5,
        price_floor: 100,
        price_ceiling: 1_000_000,
        price_half_life: 10,
        price_bump_q32: 2 << 32,
        max_regs_per_block: 100,
        param_rate_limit_blocks: 5,
        snapshot_interval: 5,
        basket_cap: 3,
    }
}

/// Applies small constants, squashing price_last down to the small floor
/// first (set_config only re-clamps, launch price_last >> small ceiling).
fn apply_small_config(contract: &mut RepoRegistry) {
    set_caller(account(1));
    let mut squash = small_constants();
    squash.price_ceiling = squash.price_floor;
    contract.set_config(squash).expect("squash config");
    contract.set_config(small_constants()).expect("set_config");
}

/// Contract with mock extension and test-friendly constants.
fn create_small_contract() -> RepoRegistry {
    register_mock();
    let mut contract = create_contract();
    apply_small_config(&mut contract);
    contract
}

fn register_repo(contract: &mut RepoRegistry, github_id: u64, name: &str, owner: AccountId) {
    set_caller(owner);
    contract
        .register(github_id, name.into(), account(2))
        .expect("register");
}

// ============================================================================
// Price curve — golden vectors (shared with the python client, task 2a-3)
// ============================================================================

const GOLDEN_HALF_LIFE: u64 = 100_800;
const GOLDEN_FLOOR: u64 = 500_000_000_000;
const GOLDEN_CEILING: u64 = 500_000_000_000_000;

/// (price_last, delta_blocks, expected_quote) at launch constants.
/// Explicit constants — the python client must reproduce these byte-for-byte.
const PRICE_GOLDEN: &[(u64, u64, u64)] = &[
    (500_000_000_000, 0, 500_000_000_000),
    (500_000_000_000, 100_800, 500_000_000_000),
    (1_000_000_000_000, 100_800, 500_000_000_000),
    (500_000_000_000_000, 1, 499_996_561_789_885),
    (500_000_000_000_000, 50_400, 353_551_803_971_640),
    (500_000_000_000_000, 100_800, 250_000_000_000_000),
    (500_000_000_000_000, 201_600, 125_000_000_000_000),
    (500_000_000_000_000, 252_000, 88_387_950_992_910),
    (500_000_000_000_000, 6_451_200, 500_000_000_000),
    (123_456_789_012_345, 12_345, 113_408_938_323_092),
    (600_000_000_000_000, 0, 500_000_000_000_000),
    (400_000_000_000, 0, 500_000_000_000),
    (2_000_000_000_000, 33_600, 1_587_396_302_726),
    (2_000_000_000_000, 302_400, 500_000_000_000),
];

/// decay_factor_q32(100_800) — golden constant shared with python.
const DECAY_FACTOR_GOLDEN: u64 = 4_294_937_762;

#[ink::test]
fn price_golden_vectors() {
    for &(last, delta, expected) in PRICE_GOLDEN {
        assert_eq!(
            crate::price::lazy_price(last, delta, GOLDEN_HALF_LIFE, GOLDEN_FLOOR, GOLDEN_CEILING),
            expected,
            "lazy_price({last}, {delta})"
        );
    }
}

#[ink::test]
fn price_decay_factor_golden() {
    assert_eq!(crate::price::decay_factor_q32(GOLDEN_HALF_LIFE), DECAY_FACTOR_GOLDEN);
}

#[ink::test]
fn price_mul_by_q32_basics() {
    assert_eq!(crate::price::mul_by_q32(1_000, crate::price::ONE_Q32), 1_000);
    assert_eq!(crate::price::mul_by_q32(1_000, crate::price::HALF_Q32), 500);
    assert_eq!(crate::price::mul_by_q32(u64::MAX, u64::MAX), u64::MAX);
    assert_eq!(crate::price::mul_by_q32(0, u64::MAX), 0);
}

#[ink::test]
fn price_pow_q32_basics() {
    assert_eq!(crate::price::pow_q32(crate::price::HALF_Q32, 0), crate::price::ONE_Q32);
    assert_eq!(crate::price::pow_q32(crate::price::HALF_Q32, 1), crate::price::HALF_Q32);
    assert_eq!(crate::price::pow_q32(crate::price::HALF_Q32, 3), 1 << 29);
}

#[ink::test]
fn price_decay_factor_edge_cases() {
    assert_eq!(crate::price::decay_factor_q32(0), crate::price::ONE_Q32);
    assert_eq!(crate::price::decay_factor_q32(1), crate::price::HALF_Q32);
}

#[ink::test]
fn price_lazy_zero_half_life_only_clamps() {
    assert_eq!(crate::price::lazy_price(700, 1_000_000, 0, 100, 500), 500);
    assert_eq!(crate::price::lazy_price(50, 1_000_000, 0, 100, 500), 100);
}

#[ink::test]
fn price_lazy_huge_delta_hits_floor() {
    let delta = GOLDEN_HALF_LIFE.saturating_mul(64);
    assert_eq!(
        crate::price::lazy_price(GOLDEN_CEILING, delta, GOLDEN_HALF_LIFE, GOLDEN_FLOOR, GOLDEN_CEILING),
        GOLDEN_FLOOR
    );
}

#[ink::test]
fn price_bump_doubles() {
    assert_eq!(crate::price::mul_by_q32(1_000, 2 << 32), 2_000);
}

// ============================================================================
// Input validators
// ============================================================================

#[ink::test]
fn full_name_accepts_valid() {
    for name in ["octo/repo", "a/b", "my-org/my.repo_x", "org7/.github", "a-b/c-d"] {
        assert!(crate::valid_full_name(name), "{name}");
    }
}

#[ink::test]
fn full_name_rejects_invalid() {
    let too_long_owner = format!("{}/r", "a".repeat(40));
    let too_long = format!("o/{}", "r".repeat(120));
    for name in [
        "", "norepo", "/repo", "owner/", "a/b/c", "Upper/repo", "owner/Repo",
        "-owner/repo", "own er/repo", "owner/re po", "owner/rep@",
        too_long_owner.as_str(), too_long.as_str(),
    ] {
        assert!(!crate::valid_full_name(name), "{name}");
    }
}

#[ink::test]
fn branch_pattern_accepts_valid() {
    let max_core = "a".repeat(39);
    let max_star = format!("{}*", "a".repeat(39));
    for pattern in ["main", "release/*", "v1.2", "feat/x_y-z", "0abc", max_core.as_str(), max_star.as_str()] {
        assert!(crate::valid_branch_pattern(pattern), "{pattern}");
    }
}

#[ink::test]
fn branch_pattern_rejects_invalid() {
    let over = "a".repeat(41);
    for pattern in ["", "*", "*main", "a*b", "Main", "-x", ".x", "a b", over.as_str()] {
        assert!(!crate::valid_branch_pattern(pattern), "{pattern}");
    }
}

#[ink::test]
fn label_validation() {
    assert!(crate::valid_label("bug"));
    assert!(crate::valid_label("good first issue"));
    assert!(crate::valid_label("priorit\u{00e9}"));
    assert!(!crate::valid_label(""));
    assert!(!crate::valid_label("a\tb"));
    assert!(!crate::valid_label(&"x".repeat(51)));
}

// ============================================================================
// Constructor / config
// ============================================================================

#[ink::test]
fn constructor_sets_defaults() {
    let contract = create_contract();
    let info = contract.get_config();
    assert_eq!(info.owner, account(1));
    assert!(!info.paused);
    assert_eq!(info.netuid, TEST_NETUID);
    assert_eq!(info.storage_version, 1);
    assert_eq!(info.repo_count, 0);
    assert_eq!(info.constants, crate::Constants::launch());
    assert_eq!(info.price_last, 500_000_000_000);
    assert_eq!(contract.get_price(), 500_000_000_000);
    assert!(contract.get_all_repos().is_empty());
    assert!(contract.get_voters().is_empty());
}

#[ink::test]
fn constructor_seeds_bounds_table() {
    let contract = create_contract();
    let all = contract.get_all_bounds();
    assert_eq!(all.len(), 26);
    assert_eq!(contract.get_bounds(1), Some(crate::Bounds { min: 0, max: 1_000_000 }));
    assert_eq!(contract.get_bounds(4), Some(crate::Bounds { min: 0, max: 200_000 }));
    assert_eq!(contract.get_bounds(9), Some(crate::Bounds { min: 1, max: 10_000_000_000 }));
    assert_eq!(contract.get_bounds(27), None);
}

#[ink::test]
fn set_config_validates() {
    let mut contract = create_contract();
    let mut bad = small_constants();
    bad.max_repos = 0;
    assert_eq!(contract.set_config(bad), Err(Error::InvalidConfig));
    let mut bad = small_constants();
    bad.price_floor = bad.price_ceiling.saturating_add(1);
    assert_eq!(contract.set_config(bad), Err(Error::InvalidConfig));
    let mut bad = small_constants();
    bad.price_bump_q32 = crate::price::ONE_Q32 - 1;
    assert_eq!(contract.set_config(bad), Err(Error::InvalidConfig));
    let mut bad = small_constants();
    bad.max_regs_per_block = 0;
    assert_eq!(contract.set_config(bad), Err(Error::InvalidConfig));
    let mut bad = small_constants();
    bad.basket_cap = 0;
    assert_eq!(contract.set_config(bad), Err(Error::InvalidConfig));
    let mut bad = small_constants();
    bad.max_repos = crate::MAX_REPOS_HARD + 1;
    assert_eq!(contract.set_config(bad), Err(Error::InvalidConfig));
    assert_eq!(contract.set_config(small_constants()), Ok(()));
    assert_eq!(contract.get_config().constants, small_constants());
}

#[ink::test]
fn set_config_requires_owner_and_reclamps_price() {
    let mut contract = create_contract();
    set_caller(account(4));
    assert_eq!(contract.set_config(small_constants()), Err(Error::NotOwner));
    set_caller(account(1));
    // Launch price_last = 500 alpha; small ceiling 1_000_000 < that -> reclamp
    contract.set_config(small_constants()).unwrap();
    assert_eq!(contract.get_config().price_last, 1_000_000);
}

// ============================================================================
// Registration
// ============================================================================

#[ink::test]
fn register_happy_path() {
    let mut contract = create_small_contract();
    set_caller(account(3));
    assert_eq!(contract.register(42, "octo/repo".into(), account(2)), Ok(()));
    let repo = contract.get_repo(42).expect("repo");
    assert_eq!(repo.github_id, 42);
    assert_eq!(repo.full_name, "octo/repo");
    assert_eq!(repo.owner, account(3));
    assert!(repo.active);
    assert_eq!(contract.get_all_repos().len(), 1);
    assert_eq!(contract.get_config().repo_count, 1);
    // Fee was floor (100); price bumped x2
    assert_eq!(contract.get_price(), 200);
}

#[ink::test]
fn register_rejects_duplicate_active() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 42, "octo/repo", account(3));
    set_caller(account(4));
    assert_eq!(
        contract.register(42, "other/name".into(), account(2)),
        Err(Error::AlreadyRegistered)
    );
}

#[ink::test]
fn register_rejects_bad_inputs() {
    let mut contract = create_small_contract();
    set_caller(account(3));
    assert_eq!(
        contract.register(0, "octo/repo".into(), account(2)),
        Err(Error::InvalidGithubId)
    );
    assert_eq!(
        contract.register(1, "NotLower/repo".into(), account(2)),
        Err(Error::InvalidFullName)
    );
    assert_eq!(
        contract.register(1, "octo/repo".into(), account(0)),
        Err(Error::ZeroAddress)
    );
}

#[ink::test]
fn register_blocked_when_paused() {
    let mut contract = create_small_contract();
    set_caller(account(1));
    contract.set_paused(true).unwrap();
    set_caller(account(3));
    assert_eq!(
        contract.register(1, "octo/repo".into(), account(2)),
        Err(Error::Paused)
    );
}

#[ink::test]
fn register_per_block_cap() {
    register_mock();
    let mut contract = create_contract();
    // Launch constants: MAX_REGS_PER_BLOCK = 1, fee = floor
    set_caller(account(3));
    assert_eq!(contract.register(1, "octo/one".into(), account(2)), Ok(()));
    assert_eq!(
        contract.register(2, "octo/two".into(), account(2)),
        Err(Error::TooManyRegistrationsThisBlock)
    );
    advance_blocks(1);
    assert_eq!(contract.register(2, "octo/two".into(), account(2)), Ok(()));
}

#[ink::test]
fn register_fee_silent_cap_shortfall_reverts() {
    test::register_chain_extension(MockExt {
        transfer_credit_cap: 99, // fee will be 100
        ..MockExt::default()
    });
    let mut contract = create_contract();
    apply_small_config(&mut contract);
    set_caller(account(3));
    assert_eq!(
        contract.register(1, "octo/repo".into(), account(2)),
        Err(Error::FeeShortfall)
    );
    assert_eq!(contract.get_repo(1), None);
    assert_eq!(contract.get_config().repo_count, 0);
    assert_eq!(contract.get_price(), 100); // no bump
}

#[ink::test]
fn register_fee_transfer_failure_reverts() {
    test::register_chain_extension(MockExt {
        transfer_status: 6, // NotEnoughStakeToWithdraw
        ..MockExt::default()
    });
    let mut contract = create_contract();
    apply_small_config(&mut contract);
    set_caller(account(3));
    assert_eq!(
        contract.register(1, "octo/repo".into(), account(2)),
        Err(Error::FeeTransferFailed)
    );
    assert_eq!(contract.get_repo(1), None);
}

#[ink::test]
fn register_recycle_failure_reverts() {
    test::register_chain_extension(MockExt {
        recycle_status: 1,
        ..MockExt::default()
    });
    let mut contract = create_contract();
    apply_small_config(&mut contract);
    set_caller(account(3));
    assert_eq!(
        contract.register(1, "octo/repo".into(), account(2)),
        Err(Error::RecycleFailed)
    );
}

#[ink::test]
fn register_recycle_shortfall_reverts() {
    test::register_chain_extension(MockExt {
        recycle_shortfall: 1,
        ..MockExt::default()
    });
    let mut contract = create_contract();
    apply_small_config(&mut contract);
    set_caller(account(3));
    assert_eq!(
        contract.register(1, "octo/repo".into(), account(2)),
        Err(Error::RecycleFailed)
    );
}

#[ink::test]
fn register_price_bumps_and_decays() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    assert_eq!(contract.get_price(), 200);
    advance_blocks(1);
    register_repo(&mut contract, 2, "octo/two", account(3));
    // Quote just below 200 after 1 block of decay, bumped x2
    let quote = crate::price::lazy_price(200, 1, 10, 100, 1_000_000);
    let expected = crate::price::mul_by_q32(quote, 2 << 32);
    assert_eq!(contract.get_price() as u64, crate::price::lazy_price(expected, 0, 10, 100, 1_000_000));
    // Full half-life returns halfway toward the floor
    advance_blocks(10);
    assert_eq!(
        contract.get_price() as u64,
        crate::price::lazy_price(expected, 10, 10, 100, 1_000_000)
    );
}

#[ink::test]
fn register_price_decays_to_floor() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    assert_eq!(contract.get_price(), 200);
    advance_blocks(20); // two half-lives: 200 -> 50 -> clamped to floor 100
    assert_eq!(contract.get_price(), 100);
}

#[ink::test]
fn reregister_after_deregister_gets_fresh_state() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract.set_param(1, 4, 100_000).unwrap();
    contract.deregister(1).unwrap();
    assert!(!contract.get_repo(1).expect("kept").active);
    advance_blocks(1);
    set_caller(account(4));
    contract.register(1, "octo/one".into(), account(2)).unwrap();
    let repo = contract.get_repo(1).expect("repo");
    assert!(repo.active);
    assert_eq!(repo.owner, account(4));
    assert_eq!(repo.reg_block, 1);
    assert!(contract.get_params(1).is_empty());
}

// ============================================================================
// Pruning
// ============================================================================

fn fill_registry(contract: &mut RepoRegistry) {
    register_repo(contract, 1, "octo/one", account(3));
    advance_blocks(1);
    register_repo(contract, 2, "octo/two", account(3));
    advance_blocks(1);
    register_repo(contract, 3, "octo/three", account(3));
}

#[ink::test]
fn prune_rejects_when_all_immune_without_fee() {
    let mut contract = create_small_contract();
    fill_registry(&mut contract);
    let price_before = contract.get_price();
    set_caller(account(4));
    assert_eq!(
        contract.register(4, "octo/four".into(), account(2)),
        Err(Error::NoPrunableSlot)
    );
    assert_eq!(contract.get_config().repo_count, 3);
    assert_eq!(contract.get_price(), price_before); // fee untouched, no bump
}

#[ink::test]
fn prune_tie_breaks_to_oldest() {
    let mut contract = create_small_contract();
    fill_registry(&mut contract); // regs at blocks 0, 1, 2; immunity 5
    advance_blocks(5); // block 7: all non-immune, no baskets -> tie -> oldest (id 1)
    set_caller(account(4));
    contract.register(4, "octo/four".into(), account(2)).unwrap();
    assert!(!contract.get_repo(1).expect("pruned").active);
    assert!(contract.get_repo(2).expect("kept").active);
    assert!(contract.get_repo(3).expect("kept").active);
    assert!(contract.get_repo(4).expect("new").active);
}

#[ink::test]
fn prune_selects_fewest_basket_appearances() {
    let mut contract = create_small_contract();
    fill_registry(&mut contract);
    set_caller(account(1));
    contract.add_voter(account(10)).unwrap();
    contract.add_voter(account(11)).unwrap();
    set_caller(account(10));
    contract.set_basket(vec![(1, 65_535)]).unwrap();
    set_caller(account(11));
    contract.set_basket(vec![(1, 30_000), (2, 35_535)]).unwrap();
    // Counts: 1 -> 2, 2 -> 1, 3 -> 0
    advance_blocks(5);
    set_caller(account(4));
    contract.register(4, "octo/four".into(), account(2)).unwrap();
    assert!(!contract.get_repo(3).expect("pruned").active);
    assert!(contract.get_repo(1).expect("kept").active);
    assert!(contract.get_repo(2).expect("kept").active);
}

#[ink::test]
fn prune_skips_immune_repos() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    advance_blocks(1);
    register_repo(&mut contract, 2, "octo/two", account(3));
    advance_blocks(5); // block 6
    register_repo(&mut contract, 3, "octo/three", account(3)); // immune until 11
    // Block 6: repo 1 (reg 0) and repo 2 (reg 1) non-immune; repo 3 immune.
    // Protect repo 1 via basket -> victim must be repo 2.
    set_caller(account(1));
    contract.add_voter(account(10)).unwrap();
    set_caller(account(10));
    contract.set_basket(vec![(1, 65_535)]).unwrap();
    set_caller(account(4));
    contract.register(4, "octo/four".into(), account(2)).unwrap();
    assert!(contract.get_repo(1).expect("kept").active);
    assert!(!contract.get_repo(2).expect("pruned").active);
    assert!(contract.get_repo(3).expect("kept").active);
}

#[ink::test]
fn prune_clears_victim_state() {
    let mut contract = create_small_contract();
    fill_registry(&mut contract);
    set_caller(account(3));
    contract.set_param(1, 4, 100_000).unwrap();
    contract
        .set_label_multiplier(1, "bug".into(), 1_500_000)
        .unwrap();
    contract.set_branch_patterns(1, vec!["main".into()]).unwrap();
    advance_blocks(5);
    set_caller(account(4));
    contract.register(4, "octo/four".into(), account(2)).unwrap();
    assert!(contract.get_params(1).is_empty());
    assert!(contract.get_label_multipliers(1).is_empty());
    assert!(contract.get_branch_patterns(1).is_empty());
}

// ============================================================================
// Hyperparams
// ============================================================================

#[ink::test]
fn set_param_happy_path() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    assert_eq!(contract.set_param(1, 4, 150_000), Ok(()));
    assert_eq!(contract.get_params(1), vec![(4, 150_000)]);
}

#[ink::test]
fn set_param_requires_repo_owner() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(4));
    assert_eq!(contract.set_param(1, 4, 150_000), Err(Error::NotRepoOwner));
}

#[ink::test]
fn set_param_unknown_key() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    assert_eq!(contract.set_param(1, 99, 1), Err(Error::UnknownParamKey));
}

#[ink::test]
fn set_param_enforces_bounds() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    // Key 2 default_label_multiplier: [500_000, 2_000_000]
    assert_eq!(contract.set_param(1, 2, 499_999), Err(Error::ValueOutOfBounds));
    assert_eq!(contract.set_param(1, 2, 2_000_001), Err(Error::ValueOutOfBounds));
    assert_eq!(contract.set_param(1, 2, 500_000), Ok(()));
    // Key 9 divisor: exclusive zero -> min 1
    advance_blocks(5);
    assert_eq!(contract.set_param(1, 9, 0), Err(Error::ValueOutOfBounds));
}

#[ink::test]
fn set_param_rate_limited_per_key() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract.set_param(1, 4, 100_000).unwrap();
    assert_eq!(contract.set_param(1, 4, 110_000), Err(Error::RateLimited));
    // Different key unaffected
    assert_eq!(contract.set_param(1, 6, 5), Ok(()));
    advance_blocks(5);
    assert_eq!(contract.set_param(1, 4, 110_000), Ok(()));
    assert_eq!(contract.get_params(1), vec![(4, 110_000), (6, 5)]);
}

#[ink::test]
fn set_param_rejects_inactive_repo() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract.deregister(1).unwrap();
    assert_eq!(contract.set_param(1, 4, 100_000), Err(Error::RepoNotActive));
    assert_eq!(contract.set_param(999, 4, 100_000), Err(Error::RepoNotFound));
}

#[ink::test]
fn set_param_blocked_when_paused() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(1));
    contract.set_paused(true).unwrap();
    set_caller(account(3));
    assert_eq!(contract.set_param(1, 4, 100_000), Err(Error::Paused));
}

// ============================================================================
// Label multipliers
// ============================================================================

#[ink::test]
fn label_multiplier_lifecycle() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract.set_label_multiplier(1, "bug".into(), 1_500_000).unwrap();
    contract.set_label_multiplier(1, "bug".into(), 800_000).unwrap(); // update in place
    contract.set_label_multiplier(1, "feature".into(), 2_000_000).unwrap();
    assert_eq!(
        contract.get_label_multipliers(1),
        vec![("bug".into(), 800_000), ("feature".into(), 2_000_000)]
    );
    contract.remove_label_multiplier(1, "bug".into()).unwrap();
    assert_eq!(contract.get_label_multipliers(1), vec![("feature".into(), 2_000_000)]);
    assert_eq!(
        contract.remove_label_multiplier(1, "bug".into()),
        Err(Error::LabelNotFound)
    );
}

#[ink::test]
fn label_multiplier_enforces_bounds_and_validity() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    assert_eq!(
        contract.set_label_multiplier(1, "bug".into(), 499_999),
        Err(Error::ValueOutOfBounds)
    );
    assert_eq!(
        contract.set_label_multiplier(1, "bug".into(), 2_000_001),
        Err(Error::ValueOutOfBounds)
    );
    assert_eq!(
        contract.set_label_multiplier(1, "".into(), 1_000_000),
        Err(Error::InvalidLabel)
    );
    assert_eq!(
        contract.set_label_multiplier(1, "x".repeat(51), 1_000_000),
        Err(Error::InvalidLabel)
    );
}

#[ink::test]
fn label_multiplier_cap() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    for i in 0..crate::MAX_LABELS {
        contract
            .set_label_multiplier(1, format!("label-{i}"), 1_000_000)
            .unwrap();
    }
    assert_eq!(
        contract.set_label_multiplier(1, "one-too-many".into(), 1_000_000),
        Err(Error::TooManyLabels)
    );
}

#[ink::test]
fn label_multiplier_requires_repo_owner() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(4));
    assert_eq!(
        contract.set_label_multiplier(1, "bug".into(), 1_000_000),
        Err(Error::NotRepoOwner)
    );
    assert_eq!(
        contract.remove_label_multiplier(1, "bug".into()),
        Err(Error::NotRepoOwner)
    );
}

// ============================================================================
// Branch patterns
// ============================================================================

#[ink::test]
fn branch_patterns_lifecycle() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract
        .set_branch_patterns(1, vec!["develop".into(), "release/*".into()])
        .unwrap();
    assert_eq!(
        contract.get_branch_patterns(1),
        vec![String::from("develop"), String::from("release/*")]
    );
    contract.set_branch_patterns(1, vec![]).unwrap(); // clears
    assert!(contract.get_branch_patterns(1).is_empty());
}

#[ink::test]
fn branch_patterns_validation() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    assert_eq!(
        contract.set_branch_patterns(1, vec!["*".into()]),
        Err(Error::InvalidPattern)
    );
    assert_eq!(
        contract.set_branch_patterns(1, vec!["main".into(), "main".into()]),
        Err(Error::DuplicatePattern)
    );
    let five = (0..5).map(|i| format!("branch-{i}")).collect();
    assert_eq!(contract.set_branch_patterns(1, five), Err(Error::TooManyPatterns));
    set_caller(account(4));
    assert_eq!(
        contract.set_branch_patterns(1, vec!["main".into()]),
        Err(Error::NotRepoOwner)
    );
}

// ============================================================================
// Rename / transfer / deregister
// ============================================================================

#[ink::test]
fn update_full_name_happy_and_rate_limited() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract.update_full_name(1, "octo/renamed".into()).unwrap();
    assert_eq!(contract.get_repo(1).expect("repo").full_name, "octo/renamed");
    assert_eq!(
        contract.update_full_name(1, "octo/again".into()),
        Err(Error::RateLimited)
    );
    advance_blocks(5);
    assert_eq!(contract.update_full_name(1, "octo/again".into()), Ok(()));
    assert_eq!(
        contract.update_full_name(1, "Bad Name".into()),
        Err(Error::InvalidFullName)
    );
}

#[ink::test]
fn transfer_ownership_happy_and_guards() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(4));
    assert_eq!(contract.transfer_ownership(1, account(4)), Err(Error::NotRepoOwner));
    set_caller(account(3));
    assert_eq!(contract.transfer_ownership(1, account(0)), Err(Error::ZeroAddress));
    contract.transfer_ownership(1, account(4)).unwrap();
    assert_eq!(contract.get_repo(1).expect("repo").owner, account(4));
    // New owner controls params now
    set_caller(account(4));
    assert_eq!(contract.set_param(1, 4, 100_000), Ok(()));
}

#[ink::test]
fn deregister_frees_slot_and_clears_state() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    contract.set_param(1, 4, 100_000).unwrap();
    contract.deregister(1).unwrap();
    assert!(!contract.get_repo(1).expect("kept").active);
    assert_eq!(contract.get_config().repo_count, 0);
    assert!(contract.get_all_repos().is_empty());
    assert!(contract.get_params(1).is_empty());
    assert_eq!(contract.deregister(1), Err(Error::RepoNotActive));
}

#[ink::test]
fn deregister_requires_repo_owner() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(4));
    assert_eq!(contract.deregister(1), Err(Error::NotRepoOwner));
}

#[ink::test]
fn force_deregister_team_only() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(4));
    assert_eq!(contract.force_deregister(1), Err(Error::NotOwner));
    set_caller(account(1));
    assert_eq!(contract.force_deregister(1), Ok(()));
    assert!(!contract.get_repo(1).expect("kept").active);
    assert_eq!(contract.force_deregister(1), Err(Error::RepoNotActive));
}

// ============================================================================
// Baskets
// ============================================================================

fn setup_with_repos_and_voter() -> RepoRegistry {
    let mut contract = create_small_contract();
    fill_registry(&mut contract);
    set_caller(account(1));
    contract.add_voter(account(10)).unwrap();
    contract
}

#[ink::test]
fn set_basket_happy_path() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(10));
    let entries = vec![(1u64, 30_000u16), (2, 20_000), (3, 15_535)];
    assert_eq!(contract.set_basket(entries.clone()), Ok(()));
    assert_eq!(contract.get_basket(account(10)), Some(entries.clone()));
    assert_eq!(contract.get_all_baskets(), vec![(account(10), entries)]);
}

#[ink::test]
fn set_basket_requires_whitelist() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(11));
    assert_eq!(
        contract.set_basket(vec![(1, 65_535)]),
        Err(Error::NotWhitelistedVoter)
    );
}

#[ink::test]
fn set_basket_validation_matrix() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(10));
    assert_eq!(contract.set_basket(vec![]), Err(Error::EmptyBasket));
    assert_eq!(
        contract.set_basket(vec![(1, 30_000), (1, 35_535)]),
        Err(Error::DuplicateBasketEntry)
    );
    assert_eq!(
        contract.set_basket(vec![(1, 65_535), (2, 0)]),
        Err(Error::ZeroWeight)
    );
    assert_eq!(
        contract.set_basket(vec![(1, 30_000), (2, 30_000)]),
        Err(Error::WeightSumMismatch)
    );
    assert_eq!(
        contract.set_basket(vec![(999, 65_535)]),
        Err(Error::RepoNotFound)
    );
}

#[ink::test]
fn set_basket_rejects_over_cap() {
    let mut contract = create_small_contract();
    // Need > basket_cap distinct repos: raise max_repos, keep cap 3
    set_caller(account(1));
    let mut constants = small_constants();
    constants.max_repos = 5;
    contract.set_config(constants).unwrap();
    for (id, name) in [(1, "o/a"), (2, "o/b"), (3, "o/c"), (4, "o/d")] {
        register_repo(&mut contract, id, name, account(3));
        advance_blocks(1);
    }
    set_caller(account(1));
    contract.add_voter(account(10)).unwrap();
    set_caller(account(10));
    assert_eq!(
        contract.set_basket(vec![(1, 20_000), (2, 20_000), (3, 20_000), (4, 5_535)]),
        Err(Error::BasketTooLarge)
    );
}

#[ink::test]
fn set_basket_rejects_inactive_repo() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(3));
    contract.deregister(2).unwrap();
    set_caller(account(10));
    assert_eq!(
        contract.set_basket(vec![(2, 65_535)]),
        Err(Error::RepoNotActive)
    );
}

#[ink::test]
fn clear_basket_removes_entry() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(10));
    contract.set_basket(vec![(1, 65_535)]).unwrap();
    contract.clear_basket().unwrap();
    assert_eq!(contract.get_basket(account(10)), None);
    assert!(contract.get_all_baskets().is_empty());
    assert_eq!(contract.clear_basket(), Ok(())); // idempotent
}

#[ink::test]
fn basket_blocked_when_paused() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(1));
    contract.set_paused(true).unwrap();
    set_caller(account(10));
    assert_eq!(contract.set_basket(vec![(1, 65_535)]), Err(Error::Paused));
    assert_eq!(contract.clear_basket(), Err(Error::Paused));
}

// ============================================================================
// Voters
// ============================================================================

#[ink::test]
fn voter_lifecycle() {
    let mut contract = create_small_contract();
    set_caller(account(1));
    contract.add_voter(account(10)).unwrap();
    assert_eq!(contract.get_voters(), vec![account(10)]);
    assert_eq!(contract.add_voter(account(10)), Err(Error::VoterAlreadyAdded));
    assert_eq!(contract.add_voter(account(0)), Err(Error::ZeroAddress));
    contract.remove_voter(account(10)).unwrap();
    assert!(contract.get_voters().is_empty());
    assert_eq!(contract.remove_voter(account(10)), Err(Error::VoterNotFound));
}

#[ink::test]
fn voter_admin_requires_owner() {
    let mut contract = create_small_contract();
    set_caller(account(4));
    assert_eq!(contract.add_voter(account(10)), Err(Error::NotOwner));
    assert_eq!(contract.remove_voter(account(10)), Err(Error::NotOwner));
}

#[ink::test]
fn voter_cap() {
    let mut contract = create_small_contract();
    set_caller(account(1));
    for i in 0..crate::MAX_VOTERS {
        contract
            .add_voter(AccountId::from([u8::try_from(i + 100).unwrap(); 32]))
            .unwrap();
    }
    assert_eq!(contract.add_voter(account(99)), Err(Error::TooManyVoters));
}

#[ink::test]
fn removed_voter_loses_basket_and_prune_weight() {
    let mut contract = setup_with_repos_and_voter();
    set_caller(account(10));
    contract.set_basket(vec![(1, 65_535)]).unwrap();
    set_caller(account(1));
    contract.remove_voter(account(10)).unwrap();
    assert_eq!(contract.get_basket(account(10)), None);
    // Repo 1 unprotected again: tie-break prunes oldest (repo 1)
    advance_blocks(7);
    set_caller(account(4));
    contract.register(4, "octo/four".into(), account(2)).unwrap();
    assert!(!contract.get_repo(1).expect("pruned").active);
}

// ============================================================================
// Bounds admin
// ============================================================================

#[ink::test]
fn set_bounds_new_key_enables_param() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(3));
    assert_eq!(contract.set_param(1, 27, 5), Err(Error::UnknownParamKey));
    set_caller(account(1));
    contract.set_bounds(27, 1, 10).unwrap();
    assert_eq!(contract.get_all_bounds().len(), 27);
    set_caller(account(3));
    assert_eq!(contract.set_param(1, 27, 5), Ok(()));
    assert_eq!(contract.set_param(1, 27, 11), Err(Error::ValueOutOfBounds));
}

#[ink::test]
fn set_bounds_guards() {
    let mut contract = create_small_contract();
    set_caller(account(4));
    assert_eq!(contract.set_bounds(27, 1, 10), Err(Error::NotOwner));
    set_caller(account(1));
    assert_eq!(contract.set_bounds(27, 10, 1), Err(Error::InvalidBounds));
    // Updating an existing key does not consume a slot
    contract.set_bounds(4, 0, 100_000).unwrap();
    assert_eq!(contract.get_all_bounds().len(), 26);
    assert_eq!(contract.get_bounds(4), Some(crate::Bounds { min: 0, max: 100_000 }));
}

#[ink::test]
fn set_bounds_key_cap() {
    let mut contract = create_small_contract();
    set_caller(account(1));
    let seeded = u8::try_from(crate::DEFAULT_BOUNDS.len()).unwrap();
    let cap = u8::try_from(crate::MAX_PARAM_KEYS).unwrap();
    for key in (seeded + 1)..=cap {
        contract.set_bounds(key, 0, 1).unwrap();
    }
    assert_eq!(contract.set_bounds(cap + 1, 0, 1), Err(Error::TooManyParamKeys));
}

// ============================================================================
// Pause / ownership / upgrade
// ============================================================================

#[ink::test]
fn pause_matrix() {
    let mut contract = create_small_contract();
    register_repo(&mut contract, 1, "octo/one", account(3));
    set_caller(account(4));
    assert_eq!(contract.set_paused(true), Err(Error::NotOwner));
    set_caller(account(1));
    contract.set_paused(true).unwrap();
    assert!(contract.get_config().paused);
    // Owner/repo-owner mutations blocked
    set_caller(account(3));
    assert_eq!(contract.deregister(1), Err(Error::Paused));
    assert_eq!(contract.update_full_name(1, "octo/x".into()), Err(Error::Paused));
    assert_eq!(contract.transfer_ownership(1, account(5)), Err(Error::Paused));
    assert_eq!(
        contract.set_label_multiplier(1, "bug".into(), 1_000_000),
        Err(Error::Paused)
    );
    assert_eq!(contract.set_branch_patterns(1, vec![]), Err(Error::Paused));
    // Team admin still works while paused
    set_caller(account(1));
    assert_eq!(contract.add_voter(account(10)), Ok(()));
    assert_eq!(contract.force_deregister(1), Ok(()));
    contract.set_paused(false).unwrap();
    assert!(!contract.get_config().paused);
}

#[ink::test]
fn set_owner_transfers_control() {
    let mut contract = create_small_contract();
    set_caller(account(4));
    assert_eq!(contract.set_owner(account(4)), Err(Error::NotOwner));
    set_caller(account(1));
    assert_eq!(contract.set_owner(account(0)), Err(Error::ZeroAddress));
    contract.set_owner(account(5)).unwrap();
    assert_eq!(contract.get_config().owner, account(5));
    assert_eq!(contract.set_paused(true), Err(Error::NotOwner)); // old owner locked out
    set_caller(account(5));
    assert_eq!(contract.set_paused(true), Ok(()));
}

#[ink::test]
fn set_code_hash_requires_owner() {
    let mut contract = create_small_contract();
    set_caller(account(4));
    assert_eq!(
        contract.set_code_hash(Hash::from([7u8; 32])),
        Err(Error::NotOwner)
    );
}

// ============================================================================
// Reads
// ============================================================================

#[ink::test]
fn get_all_repos_returns_active_only() {
    let mut contract = create_small_contract();
    fill_registry(&mut contract);
    set_caller(account(3));
    contract.deregister(2).unwrap();
    let ids: Vec<u64> = contract.get_all_repos().iter().map(|r| r.github_id).collect();
    assert_eq!(ids.len(), 2);
    assert!(ids.contains(&1) && ids.contains(&3));
}

#[ink::test]
fn get_repo_none_for_unknown() {
    let contract = create_contract();
    assert_eq!(contract.get_repo(1), None);
    assert!(contract.get_params(1).is_empty());
    assert!(contract.get_label_multipliers(1).is_empty());
    assert!(contract.get_branch_patterns(1).is_empty());
    assert_eq!(contract.get_basket(account(10)), None);
}
