use ink::prelude::string::String;
use ink::primitives::AccountId;
use scale::{Compact, Decode, Encode};

// ============================================================================
// Hard caps (compile-time, not config)
// ============================================================================

pub const MAX_REPOS_HARD: u32 = 256;
pub const MAX_VOTERS: usize = 64;
pub const MAX_PARAM_KEYS: usize = 64;
pub const MAX_LABELS: usize = 20;
pub const MAX_PATTERNS: usize = 4;
pub const MAX_FULL_NAME_LEN: usize = 100;
pub const MAX_OWNER_LEN: usize = 39;
pub const MAX_LABEL_LEN: usize = 50;
pub const MAX_PATTERN_LEN: usize = 40;
pub const LABEL_MULT_MIN: u64 = 500_000;
pub const LABEL_MULT_MAX: u64 = 2_000_000;
pub const WEIGHT_SUM: u32 = 65_535;

/// (github_id, weight) pairs; weights sum to WEIGHT_SUM
pub type BasketEntries = ink::prelude::vec::Vec<(u64, u16)>;
/// (param key, value) pairs; keys must exist in param_bounds
pub type ParamEntries = ink::prelude::vec::Vec<(u8, u64)>;
/// (label, multiplier) pairs, <= MAX_LABELS
pub type LabelEntries = ink::prelude::vec::Vec<(String, u64)>;

/// Default bounds table: (key, min, max). Fixed-point FP6 where noted in the spec.
/// Keys 9/15/19 have exclusive-zero lower bounds -> min = 1.
pub const DEFAULT_BOUNDS: [(u8, u64, u64); 26] = [
    (1, 0, 1_000_000),               // issue_discovery_share
    (2, 500_000, 2_000_000),         // default_label_multiplier
    (3, 0, 25_000_000),              // fixed_base_score (0 = unset)
    (4, 0, 200_000),                 // maintainer_cut
    (5, 0, 1),                       // trusted_label_pipeline
    (6, 1, 20),                      // min_valid_merged_prs
    (7, 300_000, 1_000_000),         // min_credibility
    (8, 0, 30),                      // excessive_pr_penalty_base_threshold
    (9, 1, 10_000_000_000),          // open_pr_threshold_token_score (divisor)
    (10, 1, 50),                     // max_open_pr_threshold
    (11, 1, 20),                     // min_valid_solved_issues
    (12, 300_000, 1_000_000),        // min_issue_credibility
    (13, 0, 1_000_000_000),          // min_token_score_for_valid_issue
    (14, 0, 30),                     // open_issue_spam_base_threshold
    (15, 1, 10_000_000_000),         // open_issue_spam_token_score_per_slot (divisor)
    (16, 1, 100),                    // max_open_issue_threshold
    (17, 1, 90),                     // pr_lookback_days
    (18, 0, 1_000_000),              // open_pr_collateral_percent
    (19, 1, 1_000_000),              // review_penalty_rate
    (20, 1_000_000, 5_000_000),      // standard_issue_multiplier
    (21, 1_000_000, 2_000_000),      // maintainer_issue_multiplier
    (22, 10_000_000, 500_000_000),   // src_tok_saturation_scale
    (23, 0, 168),                    // grace_period_hours
    (24, 1_000_000, 90_000_000),     // sigmoid_midpoint_days
    (25, 10_000, 5_000_000),         // sigmoid_steepness
    (26, 0, 1_000_000),              // time_decay_min_multiplier
];

// ============================================================================
// Chain extension types
// ============================================================================

/// StakeInfo returned by chain extension function 0.
/// Must match subtensor's StakeInfo struct exactly for SCALE decoding.
#[derive(Debug, Clone, Decode, Encode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub struct StakeInfo {
    pub hotkey: AccountId,
    pub coldkey: AccountId,
    pub netuid: Compact<u16>,
    pub stake: Compact<u64>,
    pub locked: Compact<u64>,
    pub emission: Compact<u64>,
    pub tao_emission: Compact<u64>,
    pub drain: Compact<u64>,
    pub is_registered: bool,
}

/// Non-zero chain extension status code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub struct ExtStatus(pub u32);

impl ink::env::chain_extension::FromStatusCode for ExtStatus {
    fn from_status_code(status: u32) -> Result<(), Self> {
        if status == 0 {
            Ok(())
        } else {
            Err(Self(status))
        }
    }
}

// ============================================================================
// Registry types
// ============================================================================

/// A registered repository. Keyed by immutable GitHub numeric id.
#[derive(Debug, Clone, PartialEq, Eq, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct Repo {
    pub github_id: u64,
    /// "owner/repo" lowercase, owner-updatable across renames
    pub full_name: String,
    /// Registrant coldkey; transferable
    pub owner: AccountId,
    /// Immunity anchor + prune tie-break
    pub reg_block: u64,
    pub active: bool,
}

/// Min/max bounds for a hyperparam key
#[derive(Debug, Clone, Copy, PartialEq, Eq, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct Bounds {
    pub min: u64,
    pub max: u64,
}

/// One-tx adjustable launch constants (no redeploy needed)
#[derive(Debug, Clone, PartialEq, Eq, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo, ink::storage::traits::StorageLayout))]
pub struct Constants {
    pub max_repos: u32,
    pub immunity_period: u64,
    pub price_floor: u64,
    pub price_ceiling: u64,
    pub price_half_life: u64,
    /// Post-registration price bump multiplier in Q32 (2.0 = 2 << 32)
    pub price_bump_q32: u64,
    pub max_regs_per_block: u32,
    pub param_rate_limit_blocks: u64,
    pub snapshot_interval: u64,
    pub basket_cap: u32,
}

impl Constants {
    /// Tight launch preset from the spec
    pub const fn launch() -> Self {
        Self {
            max_repos: 32,
            immunity_period: 216_000,
            price_floor: 500_000_000_000,
            price_ceiling: 500_000_000_000_000,
            price_half_life: 100_800,
            price_bump_q32: 2 << 32,
            max_regs_per_block: 1,
            param_rate_limit_blocks: 3_600,
            snapshot_interval: 3_600,
            basket_cap: 10,
        }
    }
}

/// Full contract configuration returned by get_config()
#[derive(Debug, Clone, Encode, Decode)]
#[cfg_attr(feature = "std", derive(scale_info::TypeInfo))]
pub struct RegistryInfo {
    pub owner: AccountId,
    pub paused: bool,
    pub netuid: u16,
    pub storage_version: u32,
    pub price_last: u64,
    pub price_last_block: u64,
    pub repo_count: u32,
    pub constants: Constants,
}

// ============================================================================
// Input validation
// ============================================================================

/// Lowercase "owner/repo": owner [a-z0-9-] (no leading '-', <=39),
/// repo [a-z0-9._-], total <=100 bytes.
pub fn valid_full_name(name: &str) -> bool {
    if !(3..=MAX_FULL_NAME_LEN).contains(&name.len()) {
        return false;
    }
    let Some((owner, repo)) = name.split_once('/') else {
        return false;
    };
    if owner.is_empty() || owner.len() > MAX_OWNER_LEN || repo.is_empty() || repo.contains('/') {
        return false;
    }
    let owner_ok = !owner.starts_with('-')
        && owner
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    let repo_ok = repo
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'-' | b'_' | b'.'));
    owner_ok && repo_ok
}

/// `^[a-z0-9][a-z0-9._/-]{0,38}\*?$` — bare '*' rejected, '*' only as suffix.
pub fn valid_branch_pattern(pattern: &str) -> bool {
    let bytes = pattern.as_bytes();
    if bytes.is_empty() || bytes.len() > MAX_PATTERN_LEN {
        return false;
    }
    let core = match bytes.split_last() {
        Some((b'*', rest)) => rest,
        _ => bytes,
    };
    let Some((&first, tail)) = core.split_first() else {
        return false;
    };
    (first.is_ascii_lowercase() || first.is_ascii_digit())
        && tail.len() <= 38
        && tail
            .iter()
            .all(|&b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b'/' | b'-'))
}

/// 1..=50 bytes, no ASCII control characters (multi-byte UTF-8 allowed).
pub fn valid_label(label: &str) -> bool {
    !label.is_empty() && label.len() <= MAX_LABEL_LEN && label.bytes().all(|b| b >= 0x20)
}
