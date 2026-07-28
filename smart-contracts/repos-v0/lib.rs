#![cfg_attr(not(feature = "std"), no_std, no_main)]

mod errors;
mod events;
mod price;
mod types;

pub use errors::Error;
pub use types::*;

// ============================================================================
// Chain Extension for Subtensor Staking Operations
// ============================================================================

/// Subtensor chain extension (function ids from subtensor
/// chain-extensions/src/types.rs `FunctionId`). The runtime dispatches on the
/// function id only, so the extension id matches issues-v0's convention.
#[ink::chain_extension(extension = 5001)]
pub trait SubtensorExtension {
    type ErrorCode = ExtStatus;

    /// GetStakeInfoForHotkeyColdkeyNetuidV1: pure read, always Success status.
    #[ink(function = 0, handle_status = false)]
    fn get_stake_info(hotkey: [u8; 32], coldkey: [u8; 32], netuid: u16)
        -> Option<crate::StakeInfo>;

    /// RecycleAlphaV1: destroys alpha staked on (hotkey, contract coldkey).
    /// Origin = the contract account. Returns the amount actually recycled.
    #[ink(function = 16)]
    fn recycle_alpha(hotkey: [u8; 32], netuid: u16, amount: u64) -> u64;

    /// CallerTransferStakeV1: dispatches transfer_stake as the ORIGINAL tx
    /// signer, moving their alpha on `hotkey` to `destination_coldkey`.
    #[ink(function = 25)]
    fn caller_transfer_stake(
        destination_coldkey: [u8; 32],
        hotkey: [u8; 32],
        origin_netuid: u16,
        destination_netuid: u16,
        amount: u64,
    );
}

/// Custom environment with Subtensor chain extension.
#[derive(Debug, Clone, PartialEq, Eq)]
#[ink::scale_derive(TypeInfo)]
pub enum CustomEnvironment {}

impl ink::env::Environment for CustomEnvironment {
    const MAX_EVENT_TOPICS: usize = 4;
    type AccountId = ink::primitives::AccountId;
    type Balance = u128;
    type Hash = ink::primitives::Hash;
    type Timestamp = u64;
    type BlockNumber = u32;
    type ChainExtension = SubtensorExtension;
}

#[ink::contract(env = crate::CustomEnvironment)]
mod repo_registry {
    use crate::events::*;
    use crate::price;
    use crate::types::*;
    use crate::Error;
    use ink::prelude::string::String;
    use ink::prelude::vec::Vec;
    use ink::storage::Mapping;

    const ZERO_ACCOUNT: [u8; 32] = [0u8; 32];

    /// Repository registry: whitelist, per-repo hyperparams, validator baskets.
    ///
    /// # Storage layout (python childstate reader contract)
    ///
    /// Root cell = SCALE-encoded packed fields in declaration order. Fixed
    /// byte offsets (all ints little-endian):
    ///
    /// | offset | field                | type      |
    /// |--------|----------------------|-----------|
    /// | 0      | owner                | AccountId (32) |
    /// | 32     | paused               | bool (1)  |
    /// | 33     | netuid               | u16 (2)   |
    /// | 35     | storage_version      | u32 (4)   |
    /// | 39     | price_last           | u64 (8)   |
    /// | 47     | price_last_block     | u64 (8)   |
    /// | 55     | last_reg_block       | u64 (8)   |
    /// | 63     | regs_in_block        | u32 (4)   |
    /// | 67     | constants            | Constants (68: max_repos u32, immunity_period u64, price_floor u64, price_ceiling u64, price_half_life u64, price_bump_q32 u64, max_regs_per_block u32, param_rate_limit_blocks u64, snapshot_interval u64, basket_cap u32) |
    /// | 135    | repo_ids             | Vec<u64> (compact len + n*8) |
    /// | var    | voters               | Vec<AccountId> (compact len + n*32) |
    /// | var    | bound_keys           | Vec<u8> (compact len + n) |
    ///
    /// Mappings are lazy cells under ink AutoKey; their root keys come from
    /// metadata.json (generated in the python client task). Reads are pure
    /// storage — dry-run/childstate safe.
    #[ink(storage)]
    pub struct RepoRegistry {
        owner: AccountId,
        paused: bool,
        netuid: u16,
        storage_version: u32,
        price_last: u64,
        price_last_block: u64,
        last_reg_block: u64,
        regs_in_block: u32,
        constants: Constants,
        /// Active repo ids, bounded by constants.max_repos (<= MAX_REPOS_HARD)
        repo_ids: Vec<u64>,
        /// Team whitelist of validator hotkeys (basket gate + prune counts)
        voters: Vec<AccountId>,
        /// Keys present in param_bounds, bounded by MAX_PARAM_KEYS
        bound_keys: Vec<u8>,
        repos: Mapping<u64, Repo>,
        /// (key, value) pairs per repo; keys must exist in param_bounds
        params: Mapping<u64, ParamEntries>,
        param_bounds: Mapping<u8, Bounds>,
        /// (label, multiplier) pairs per repo, <= MAX_LABELS
        label_mults: Mapping<u64, LabelEntries>,
        /// <= MAX_PATTERNS validated patterns per repo
        branch_patterns: Mapping<u64, Vec<String>>,
        /// hotkey -> basket entries, <= constants.basket_cap, weights sum 65535
        baskets: Mapping<AccountId, BasketEntries>,
        /// (github_id, key) -> block of last change (rate limit)
        last_param_change: Mapping<(u64, u8), u64>,
        /// github_id -> block of last rename (rate limit)
        last_rename: Mapping<u64, u64>,
    }

    impl RepoRegistry {
        // ====================================================================
        // Constructor
        // ====================================================================

        #[ink(constructor)]
        pub fn new(owner: AccountId, netuid: u16) -> Self {
            let mut param_bounds = Mapping::default();
            let mut bound_keys = Vec::new();
            for (key, min, max) in DEFAULT_BOUNDS {
                param_bounds.insert(key, &Bounds { min, max });
                bound_keys.push(key);
            }
            let constants = Constants::launch();
            Self {
                owner,
                paused: false,
                netuid,
                storage_version: 1,
                price_last: constants.price_floor,
                price_last_block: u64::from(Self::env().block_number()),
                last_reg_block: 0,
                regs_in_block: 0,
                constants,
                repo_ids: Vec::new(),
                voters: Vec::new(),
                bound_keys,
                repos: Mapping::default(),
                params: Mapping::default(),
                param_bounds,
                label_mults: Mapping::default(),
                branch_patterns: Mapping::default(),
                baskets: Mapping::default(),
                last_param_change: Mapping::default(),
                last_rename: Mapping::default(),
            }
        }

        // ====================================================================
        // Permissionless
        // ====================================================================

        /// Registers a repository, pulling the dynamic alpha fee from the
        /// caller's stake on `fee_hotkey` and recycling it. Prunes the least
        /// supported non-immune repo when the registry is full.
        #[ink(message)]
        pub fn register(
            &mut self,
            github_id: u64,
            full_name: String,
            fee_hotkey: AccountId,
        ) -> Result<(), Error> {
            self.ensure_not_paused()?;
            if github_id == 0 {
                return Err(Error::InvalidGithubId);
            }
            if !valid_full_name(&full_name) {
                return Err(Error::InvalidFullName);
            }
            if fee_hotkey == AccountId::from(ZERO_ACCOUNT) {
                return Err(Error::ZeroAddress);
            }
            let now = self.now();
            if now == self.last_reg_block && self.regs_in_block >= self.constants.max_regs_per_block
            {
                return Err(Error::TooManyRegistrationsThisBlock);
            }
            if let Some(existing) = self.repos.get(github_id) {
                if existing.active {
                    return Err(Error::AlreadyRegistered);
                }
            }
            // Prune feasibility BEFORE the fee: all-immune must reject with no fee taken
            let full = u32::try_from(self.repo_ids.len()).unwrap_or(u32::MAX)
                >= self.constants.max_repos;
            let victim = if full { Some(self.select_victim(now)?) } else { None };

            let fee = self.current_price(now);
            self.pull_and_recycle_fee(fee_hotkey, fee)?;

            if let Some(victim_id) = victim {
                if let Some(pruned) = self.remove_repo(victim_id) {
                    self.env().emit_event(RepositoryPruned {
                        github_id: victim_id,
                        full_name: pruned.full_name,
                        replaced_by: github_id,
                    });
                }
            }

            let caller = self.env().caller();
            let repo = Repo {
                github_id,
                full_name: full_name.clone(),
                owner: caller,
                reg_block: now,
                active: true,
            };
            self.repos.insert(github_id, &repo);
            self.repo_ids.push(github_id);

            let bumped = price::mul_by_q32(fee, self.constants.price_bump_q32);
            self.price_last = core::cmp::max(
                self.constants.price_floor,
                core::cmp::min(bumped, self.constants.price_ceiling),
            );
            self.price_last_block = now;
            if now == self.last_reg_block {
                self.regs_in_block = self.regs_in_block.saturating_add(1);
            } else {
                self.last_reg_block = now;
                self.regs_in_block = 1;
            }

            self.env().emit_event(RepositoryRegistered {
                github_id,
                full_name,
                owner: caller,
                fee_paid: fee,
                reg_block: now,
            });
            Ok(())
        }

        /// Current registration price quote (lazy decay, pure storage read).
        #[ink(message)]
        pub fn get_price(&self) -> u128 {
            u128::from(self.current_price(self.now()))
        }

        // ====================================================================
        // Repo owner
        // ====================================================================

        #[ink(message)]
        pub fn set_param(&mut self, github_id: u64, key: u8, value: u64) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            let bounds = self.param_bounds.get(key).ok_or(Error::UnknownParamKey)?;
            if value < bounds.min || value > bounds.max {
                return Err(Error::ValueOutOfBounds);
            }
            let now = self.now();
            self.check_rate_limit(self.last_param_change.get((github_id, key)), now)?;

            let mut entries = self.params.get(github_id).unwrap_or_default();
            match entries.iter_mut().find(|(k, _)| *k == key) {
                Some(entry) => entry.1 = value,
                None => entries.push((key, value)),
            }
            self.params.insert(github_id, &entries);
            self.last_param_change.insert((github_id, key), &now);
            self.env().emit_event(ParamSet { github_id, key, value });
            Ok(())
        }

        #[ink(message)]
        pub fn set_label_multiplier(
            &mut self,
            github_id: u64,
            label: String,
            value: u64,
        ) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            if !valid_label(&label) {
                return Err(Error::InvalidLabel);
            }
            if !(LABEL_MULT_MIN..=LABEL_MULT_MAX).contains(&value) {
                return Err(Error::ValueOutOfBounds);
            }
            let mut entries = self.label_mults.get(github_id).unwrap_or_default();
            match entries.iter_mut().find(|(l, _)| *l == label) {
                Some(entry) => entry.1 = value,
                None => {
                    if entries.len() >= MAX_LABELS {
                        return Err(Error::TooManyLabels);
                    }
                    entries.push((label.clone(), value));
                }
            }
            self.label_mults.insert(github_id, &entries);
            self.env().emit_event(LabelMultiplierSet { github_id, label, value });
            Ok(())
        }

        #[ink(message)]
        pub fn remove_label_multiplier(
            &mut self,
            github_id: u64,
            label: String,
        ) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            let mut entries = self.label_mults.get(github_id).unwrap_or_default();
            let pos = entries
                .iter()
                .position(|(l, _)| *l == label)
                .ok_or(Error::LabelNotFound)?;
            entries.remove(pos);
            if entries.is_empty() {
                self.label_mults.remove(github_id);
            } else {
                self.label_mults.insert(github_id, &entries);
            }
            self.env().emit_event(LabelMultiplierRemoved { github_id, label });
            Ok(())
        }

        /// Replaces the full pattern list. Empty list clears.
        #[ink(message)]
        pub fn set_branch_patterns(
            &mut self,
            github_id: u64,
            patterns: Vec<String>,
        ) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            if patterns.len() > MAX_PATTERNS {
                return Err(Error::TooManyPatterns);
            }
            for (i, pattern) in patterns.iter().enumerate() {
                if !valid_branch_pattern(pattern) {
                    return Err(Error::InvalidPattern);
                }
                if patterns.iter().take(i).any(|other| other == pattern) {
                    return Err(Error::DuplicatePattern);
                }
            }
            if patterns.is_empty() {
                self.branch_patterns.remove(github_id);
            } else {
                self.branch_patterns.insert(github_id, &patterns);
            }
            self.env().emit_event(BranchPatternsSet { github_id, patterns });
            Ok(())
        }

        /// Follows a GitHub rename; id stays stable. Rate-limited.
        #[ink(message)]
        pub fn update_full_name(
            &mut self,
            github_id: u64,
            full_name: String,
        ) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let mut repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            if !valid_full_name(&full_name) {
                return Err(Error::InvalidFullName);
            }
            let now = self.now();
            self.check_rate_limit(self.last_rename.get(github_id), now)?;
            let old_full_name = repo.full_name;
            repo.full_name = full_name.clone();
            self.repos.insert(github_id, &repo);
            self.last_rename.insert(github_id, &now);
            self.env().emit_event(FullNameUpdated {
                github_id,
                old_full_name,
                new_full_name: full_name,
            });
            Ok(())
        }

        #[ink(message)]
        pub fn transfer_ownership(
            &mut self,
            github_id: u64,
            new_owner: AccountId,
        ) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let mut repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            if new_owner == AccountId::from(ZERO_ACCOUNT) {
                return Err(Error::ZeroAddress);
            }
            let old_owner = repo.owner;
            repo.owner = new_owner;
            self.repos.insert(github_id, &repo);
            self.env().emit_event(RepoOwnershipTransferred {
                github_id,
                old_owner,
                new_owner,
            });
            Ok(())
        }

        /// Frees the slot. No refund. Re-registration pays a fresh fee.
        #[ink(message)]
        pub fn deregister(&mut self, github_id: u64) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let repo = self.active_repo(github_id)?;
            self.ensure_repo_owner(&repo)?;
            self.remove_and_emit(github_id, false);
            Ok(())
        }

        // ====================================================================
        // Whitelisted validator hotkey
        // ====================================================================

        /// All ids must be registered+active, no duplicates, weights sum 65535.
        #[ink(message)]
        pub fn set_basket(&mut self, entries: BasketEntries) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let caller = self.env().caller();
            if !self.voters.contains(&caller) {
                return Err(Error::NotWhitelistedVoter);
            }
            self.validate_basket(&entries)?;
            self.baskets.insert(caller, &entries);
            self.env().emit_event(BasketSet { hotkey: caller, entries });
            Ok(())
        }

        #[ink(message)]
        pub fn clear_basket(&mut self) -> Result<(), Error> {
            self.ensure_not_paused()?;
            let caller = self.env().caller();
            if self.baskets.get(caller).is_some() {
                self.baskets.remove(caller);
                self.env().emit_event(BasketCleared { hotkey: caller });
            }
            Ok(())
        }

        // ====================================================================
        // Team (contract owner)
        // ====================================================================

        #[ink(message)]
        pub fn add_voter(&mut self, hotkey: AccountId) -> Result<(), Error> {
            self.ensure_owner()?;
            if hotkey == AccountId::from(ZERO_ACCOUNT) {
                return Err(Error::ZeroAddress);
            }
            if self.voters.contains(&hotkey) {
                return Err(Error::VoterAlreadyAdded);
            }
            if self.voters.len() >= MAX_VOTERS {
                return Err(Error::TooManyVoters);
            }
            self.voters.push(hotkey);
            self.env().emit_event(VoterAdded { hotkey });
            Ok(())
        }

        #[ink(message)]
        pub fn remove_voter(&mut self, hotkey: AccountId) -> Result<(), Error> {
            self.ensure_owner()?;
            let pos = self
                .voters
                .iter()
                .position(|voter| voter == &hotkey)
                .ok_or(Error::VoterNotFound)?;
            self.voters.remove(pos);
            self.baskets.remove(hotkey);
            self.env().emit_event(VoterRemoved { hotkey });
            Ok(())
        }

        #[ink(message)]
        pub fn set_bounds(&mut self, key: u8, min: u64, max: u64) -> Result<(), Error> {
            self.ensure_owner()?;
            if min > max {
                return Err(Error::InvalidBounds);
            }
            if !self.bound_keys.contains(&key) {
                if self.bound_keys.len() >= MAX_PARAM_KEYS {
                    return Err(Error::TooManyParamKeys);
                }
                self.bound_keys.push(key);
            }
            self.param_bounds.insert(key, &Bounds { min, max });
            self.env().emit_event(BoundsSet { key, min, max });
            Ok(())
        }

        /// Replaces the adjustable constants. price_last is re-clamped into
        /// the new [floor, ceiling] band.
        #[ink(message)]
        pub fn set_config(&mut self, constants: Constants) -> Result<(), Error> {
            self.ensure_owner()?;
            let valid = constants.max_repos >= 1
                && constants.max_repos <= MAX_REPOS_HARD
                && constants.price_floor >= 1
                && constants.price_floor <= constants.price_ceiling
                && constants.price_bump_q32 >= price::ONE_Q32
                && constants.max_regs_per_block >= 1
                && constants.basket_cap >= 1;
            if !valid {
                return Err(Error::InvalidConfig);
            }
            self.price_last = core::cmp::max(
                constants.price_floor,
                core::cmp::min(self.price_last, constants.price_ceiling),
            );
            self.constants = constants;
            self.env().emit_event(ConfigUpdated {});
            Ok(())
        }

        /// Team escape hatch (squat-guard mismatch, abuse).
        #[ink(message)]
        pub fn force_deregister(&mut self, github_id: u64) -> Result<(), Error> {
            self.ensure_owner()?;
            self.active_repo(github_id)?;
            self.remove_and_emit(github_id, true);
            Ok(())
        }

        #[ink(message)]
        pub fn set_paused(&mut self, paused: bool) -> Result<(), Error> {
            self.ensure_owner()?;
            self.paused = paused;
            self.env().emit_event(PausedSet { paused });
            Ok(())
        }

        #[ink(message)]
        pub fn set_owner(&mut self, new_owner: AccountId) -> Result<(), Error> {
            self.ensure_owner()?;
            if new_owner == AccountId::from(ZERO_ACCOUNT) {
                return Err(Error::ZeroAddress);
            }
            let old_owner = self.owner;
            self.owner = new_owner;
            self.env().emit_event(OwnerChanged { old_owner, new_owner });
            Ok(())
        }

        /// Upgrades the contract logic in place (storage preserved).
        #[ink(message)]
        pub fn set_code_hash(&mut self, code_hash: Hash) -> Result<(), Error> {
            self.ensure_owner()?;
            self.env()
                .set_code_hash(&code_hash)
                .map_err(|_| Error::SetCodeFailed)?;
            self.env().emit_event(CodeHashSet { code_hash });
            Ok(())
        }

        // ====================================================================
        // Reads (all pure storage — dry-run safe)
        // ====================================================================

        #[ink(message)]
        pub fn get_repo(&self, github_id: u64) -> Option<Repo> {
            self.repos.get(github_id)
        }

        #[ink(message)]
        pub fn get_all_repos(&self) -> Vec<Repo> {
            self.repo_ids
                .iter()
                .filter_map(|id| self.repos.get(id))
                .collect()
        }

        #[ink(message)]
        pub fn get_params(&self, github_id: u64) -> ParamEntries {
            self.params.get(github_id).unwrap_or_default()
        }

        #[ink(message)]
        pub fn get_label_multipliers(&self, github_id: u64) -> LabelEntries {
            self.label_mults.get(github_id).unwrap_or_default()
        }

        #[ink(message)]
        pub fn get_branch_patterns(&self, github_id: u64) -> Vec<String> {
            self.branch_patterns.get(github_id).unwrap_or_default()
        }

        #[ink(message)]
        pub fn get_basket(&self, hotkey: AccountId) -> Option<BasketEntries> {
            self.baskets.get(hotkey)
        }

        #[ink(message)]
        pub fn get_all_baskets(&self) -> Vec<(AccountId, BasketEntries)> {
            self.voters
                .iter()
                .filter_map(|voter| self.baskets.get(voter).map(|basket| (*voter, basket)))
                .collect()
        }

        #[ink(message)]
        pub fn get_voters(&self) -> Vec<AccountId> {
            self.voters.clone()
        }

        #[ink(message)]
        pub fn get_bounds(&self, key: u8) -> Option<Bounds> {
            self.param_bounds.get(key)
        }

        #[ink(message)]
        pub fn get_all_bounds(&self) -> Vec<(u8, Bounds)> {
            self.bound_keys
                .iter()
                .filter_map(|key| self.param_bounds.get(key).map(|bounds| (*key, bounds)))
                .collect()
        }

        #[ink(message)]
        pub fn get_config(&self) -> RegistryInfo {
            RegistryInfo {
                owner: self.owner,
                paused: self.paused,
                netuid: self.netuid,
                storage_version: self.storage_version,
                price_last: self.price_last,
                price_last_block: self.price_last_block,
                repo_count: u32::try_from(self.repo_ids.len()).unwrap_or(u32::MAX),
                constants: self.constants.clone(),
            }
        }

        // ====================================================================
        // Internal
        // ====================================================================

        fn now(&self) -> u64 {
            u64::from(self.env().block_number())
        }

        fn ensure_owner(&self) -> Result<(), Error> {
            if self.env().caller() != self.owner {
                return Err(Error::NotOwner);
            }
            Ok(())
        }

        fn ensure_not_paused(&self) -> Result<(), Error> {
            if self.paused {
                return Err(Error::Paused);
            }
            Ok(())
        }

        fn ensure_repo_owner(&self, repo: &Repo) -> Result<(), Error> {
            if self.env().caller() != repo.owner {
                return Err(Error::NotRepoOwner);
            }
            Ok(())
        }

        fn active_repo(&self, github_id: u64) -> Result<Repo, Error> {
            let repo = self.repos.get(github_id).ok_or(Error::RepoNotFound)?;
            if !repo.active {
                return Err(Error::RepoNotActive);
            }
            Ok(repo)
        }

        fn check_rate_limit(&self, last: Option<u64>, now: u64) -> Result<(), Error> {
            match last {
                Some(block)
                    if now.saturating_sub(block) < self.constants.param_rate_limit_blocks =>
                {
                    Err(Error::RateLimited)
                }
                _ => Ok(()),
            }
        }

        fn current_price(&self, now: u64) -> u64 {
            price::lazy_price(
                self.price_last,
                now.saturating_sub(self.price_last_block),
                self.constants.price_half_life,
                self.constants.price_floor,
                self.constants.price_ceiling,
            )
        }

        /// Pull `fee` alpha from the caller's stake on `hotkey` into the
        /// contract, verify the delta (silent-cap guard), then recycle it.
        /// Any shortfall reverts the whole message.
        fn pull_and_recycle_fee(&mut self, hotkey: AccountId, fee: u64) -> Result<(), Error> {
            if fee == 0 {
                return Ok(());
            }
            let contract: [u8; 32] = *self.env().account_id().as_ref();
            let hot: [u8; 32] = *hotkey.as_ref();
            let before = self.stake_of(hot, contract);
            self.env()
                .extension()
                .caller_transfer_stake(contract, hot, self.netuid, self.netuid, fee)
                .map_err(|_| Error::FeeTransferFailed)?;
            let after = self.stake_of(hot, contract);
            if after.saturating_sub(before) < fee {
                return Err(Error::FeeShortfall);
            }
            let recycled = self
                .env()
                .extension()
                .recycle_alpha(hot, self.netuid, fee)
                .map_err(|_| Error::RecycleFailed)?;
            if recycled < fee {
                return Err(Error::RecycleFailed);
            }
            Ok(())
        }

        fn stake_of(&self, hotkey: [u8; 32], coldkey: [u8; 32]) -> u64 {
            self.env()
                .extension()
                .get_stake_info(hotkey, coldkey, self.netuid)
                .map(|info| info.stake.0)
                .unwrap_or(0)
        }

        /// 2a prune selection: victim = non-immune repo in fewest whitelisted
        /// baskets; tie -> lowest reg_block. All immune -> NoPrunableSlot.
        /// (2b swaps this to stake-weighted selection; storage unchanged.)
        fn select_victim(&self, now: u64) -> Result<u64, Error> {
            let mut best: Option<(u32, u64, u64)> = None;
            for &id in &self.repo_ids {
                let Some(repo) = self.repos.get(id) else { continue };
                if now < repo.reg_block.saturating_add(self.constants.immunity_period) {
                    continue;
                }
                let count = self.basket_count(id);
                let candidate = (count, repo.reg_block, id);
                let better = match best {
                    None => true,
                    Some((best_count, best_block, _)) => {
                        count < best_count || (count == best_count && repo.reg_block < best_block)
                    }
                };
                if better {
                    best = Some(candidate);
                }
            }
            best.map(|(_, _, id)| id).ok_or(Error::NoPrunableSlot)
        }

        /// Number of whitelisted baskets containing `github_id`.
        fn basket_count(&self, github_id: u64) -> u32 {
            let mut count = 0u32;
            for voter in &self.voters {
                if let Some(basket) = self.baskets.get(voter) {
                    if basket.iter().any(|(id, _)| *id == github_id) {
                        count = count.saturating_add(1);
                    }
                }
            }
            count
        }

        fn validate_basket(&self, entries: &[(u64, u16)]) -> Result<(), Error> {
            if entries.is_empty() {
                return Err(Error::EmptyBasket);
            }
            if entries.len() > self.constants.basket_cap as usize {
                return Err(Error::BasketTooLarge);
            }
            let mut sum = 0u32;
            for (i, (id, weight)) in entries.iter().enumerate() {
                if *weight == 0 {
                    return Err(Error::ZeroWeight);
                }
                if entries.iter().take(i).any(|(other, _)| other == id) {
                    return Err(Error::DuplicateBasketEntry);
                }
                self.active_repo(*id)?;
                sum = sum.saturating_add(u32::from(*weight));
            }
            if sum != WEIGHT_SUM {
                return Err(Error::WeightSumMismatch);
            }
            Ok(())
        }

        /// Deactivates a repo and clears its per-repo state. The Repo record
        /// stays (active = false) for history; stale basket entries remain and
        /// fail re-validation on the next set_basket.
        fn remove_repo(&mut self, github_id: u64) -> Option<Repo> {
            let mut repo = self.repos.get(github_id)?;
            repo.active = false;
            self.repos.insert(github_id, &repo);
            if let Some(pos) = self.repo_ids.iter().position(|&id| id == github_id) {
                self.repo_ids.swap_remove(pos);
            }
            if let Some(entries) = self.params.get(github_id) {
                for (key, _) in entries {
                    self.last_param_change.remove((github_id, key));
                }
                self.params.remove(github_id);
            }
            self.label_mults.remove(github_id);
            self.branch_patterns.remove(github_id);
            self.last_rename.remove(github_id);
            Some(repo)
        }

        fn remove_and_emit(&mut self, github_id: u64, forced: bool) {
            if let Some(removed) = self.remove_repo(github_id) {
                self.env().emit_event(RepositoryDeregistered {
                    github_id,
                    full_name: removed.full_name,
                    forced,
                });
            }
        }
    }

    #[cfg(test)]
    mod tests {
        include!("tests.rs");
    }
}
