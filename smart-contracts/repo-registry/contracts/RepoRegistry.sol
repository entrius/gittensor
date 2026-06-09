// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.30;

import {Ownable2StepUpgradeable} from "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

import {IStaking, IAddressMapping, ISTAKING_ADDRESS, IADDRESS_MAPPING_ADDRESS} from "./precompiles/ISubtensor.sol";

/// @title RepoRegistry
/// @notice On-chain registry of GitHub repositories for the Gittensor subnet.
///         Owners register repos and commit real ALPHA stake; the contract is
///         the single coldkey custodying all stake (one registry hotkey, one
///         netuid) and keeps a per-repo/per-staker ledger of the ALPHA minted.
///         Normalized stake drives each repo's emission share. Scoring
///         hyperparameters live off-chain (gittensor-db); only stake/ownership
///         is on-chain. A registered repo is live immediately (no activation).
/// @dev    Phase 3: stake is real ALPHA via the 0x805 precompile (contract is
///         the coldkey). Callers send native TAO; the contract stakes it to the
///         registry hotkey and records the ALPHA minted (getStake delta).
///         Deferred: withdrawal/cooldown, burn + eviction (P4), GitHub
///         ownership proof (P6). See REPO_REGISTRY_DESIGN.md at the repo root.
contract RepoRegistry is Initializable, Ownable2StepUpgradeable, UUPSUpgradeable {
    // ─── Parameters (const for now; promoted to owner-settable storage later) ──

    /// @notice One-time fee paid at registration, in native TAO (wei). Retained
    ///         as the contract's free balance (not staked).
    uint256 public constant REGISTRATION_FEE = 1 ether;
    /// @notice Minimum TAO (wei) that must be committed as stake at registration.
    uint256 public constant MIN_STAKE_FLOOR = 10 ether;
    /// @notice Blocks a freshly registered repo is protected from eviction (~1 day).
    uint64 public constant IMMUNITY_BLOCKS = 7200;
    /// @notice Hard cap on registered repos (competitive eviction replaces this later).
    uint256 public constant MAX_REPOS = 256;
    /// @notice Fixed-point scale for emission share (1e18 == 100%).
    uint256 public constant WEIGHT_PRECISION = 1e18;
    /// @notice EVM TAO precision is 1e18; Substrate stake precision is 1e9 (RAO).
    uint256 public constant WEI_PER_RAO = 1e9;

    // ─── Subnet custody config (set at init) ──────────────────────────────────

    /// @notice Hotkey all registry stake is delegated to on `netuid`.
    bytes32 public registryHotkey;
    /// @notice Subnet the registry's ALPHA stake lives on.
    uint256 public netuid;
    /// @notice This contract's own SS58 coldkey (cached from the address-mapping
    ///         precompile) — the coldkey that owns all staked ALPHA.
    bytes32 public selfColdkey;

    // ─── State ──────────────────────────────────────────────────────────────

    struct Repo {
        address owner; // registrant (GitHub ownership proof lands in P6)
        string fullName; // canonical "owner/name"
        uint256 totalStake; // ALPHA credited to this repo
        uint64 registeredAtBlock; // for the later EMA price/cap calc
        uint64 immunityUntil; // block until which the repo can't be evicted
        bool exists;
    }

    /// @notice Number of repositories currently registered.
    uint256 public registeredRepoCount;
    /// @notice Sum of ALPHA stake across all repos — denominator for emission share.
    uint256 public grandTotalStake;
    /// @notice Accumulated registration fees in TAO (wei); destination TBD.
    uint256 public collectedFees;

    mapping(bytes32 => Repo) private _repos;
    mapping(bytes32 => mapping(address => uint256)) private _stakes; // ALPHA per staker
    bytes32[] private _repoIds;

    // ─── Events ─────────────────────────────────────────────────────────────

    event RepoRegistered(
        bytes32 indexed repoId,
        address indexed owner,
        string fullName,
        uint256 stakeAlpha,
        uint256 atBlock
    );
    event Staked(
        bytes32 indexed repoId,
        address indexed staker,
        uint256 stakeAlpha,
        uint256 newTotalStake
    );

    // ─── Errors ─────────────────────────────────────────────────────────────

    error InvalidName(); // empty, missing/extra "/", or empty owner/name side
    error AlreadyRegistered();
    error NotRegistered();
    error RegistryFull();
    error InsufficientPayment(); // below REGISTRATION_FEE + MIN_STAKE_FLOOR
    error ZeroAmount();
    error NoStakeMinted(); // addStake produced zero ALPHA

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /// @notice Initialize the proxy.
    /// @param initialOwner   Address controlling upgrades and owner-only params.
    /// @param registryHotkey_ Hotkey (32-byte pubkey) all stake is delegated to.
    /// @param netuid_        Subnet the stake lives on.
    function initialize(
        address initialOwner,
        bytes32 registryHotkey_,
        uint256 netuid_
    ) external initializer {
        __Ownable_init(initialOwner);
        __Ownable2Step_init();
        registryHotkey = registryHotkey_;
        netuid = netuid_;
        // Resolve and cache this contract's coldkey (the proxy address mapped to
        // SS58). Used as the coldkey arg to getStake when measuring ALPHA minted.
        selfColdkey = IAddressMapping(IADDRESS_MAPPING_ADDRESS).addressMapping(address(this));
    }

    /// @notice Implementation version, bumped on each upgrade.
    function version() external pure returns (string memory) {
        return "0.3.0";
    }

    // ─── Registration & staking ───────────────────────────────────────────────

    /// @notice Register a repository, paying the fee and committing initial stake.
    /// @dev    Phase 1/3: unauthenticated (owner = msg.sender). msg.value must
    ///         cover REGISTRATION_FEE + at least MIN_STAKE_FLOOR; the remainder
    ///         is staked as ALPHA to the registry hotkey.
    /// @param fullName GitHub "owner/name".
    /// @return repoId keccak256 id of the canonicalized name.
    function registerRepo(string calldata fullName) external payable returns (bytes32 repoId) {
        if (_repoIds.length >= MAX_REPOS) revert RegistryFull();

        // Canonicalize: GitHub names are case-insensitive, so lowercase before
        // hashing to collapse "Foo/Bar" and "foo/bar" to one id (anti-dup).
        string memory name = _toLower(fullName);
        if (!_isValidName(name)) revert InvalidName();

        repoId = keccak256(bytes(name));
        if (_repos[repoId].exists) revert AlreadyRegistered();
        if (msg.value < REGISTRATION_FEE + MIN_STAKE_FLOOR) revert InsufficientPayment();

        collectedFees += REGISTRATION_FEE;
        uint256 stakeAlpha = _stakeToRegistry(msg.value - REGISTRATION_FEE);

        uint64 nowBlock = uint64(block.number);
        _repos[repoId] = Repo({
            owner: msg.sender,
            fullName: name,
            totalStake: stakeAlpha,
            registeredAtBlock: nowBlock,
            immunityUntil: nowBlock + IMMUNITY_BLOCKS,
            exists: true
        });
        _stakes[repoId][msg.sender] = stakeAlpha;
        _repoIds.push(repoId);
        registeredRepoCount = _repoIds.length;
        grandTotalStake += stakeAlpha;

        emit RepoRegistered(repoId, msg.sender, name, stakeAlpha, nowBlock);
    }

    /// @notice Add stake to a registered repo. Anyone may stake toward any repo.
    ///         The sent TAO is staked as ALPHA to the registry hotkey.
    function stake(bytes32 repoId) external payable {
        Repo storage repo = _repos[repoId];
        if (!repo.exists) revert NotRegistered();
        if (msg.value == 0) revert ZeroAmount();

        uint256 stakeAlpha = _stakeToRegistry(msg.value);
        _stakes[repoId][msg.sender] += stakeAlpha;
        repo.totalStake += stakeAlpha;
        grandTotalStake += stakeAlpha;

        emit Staked(repoId, msg.sender, stakeAlpha, repo.totalStake);
    }

    /// @dev Stake `weiAmount` of native TAO to the registry hotkey via the 0x805
    ///      precompile (this contract is the coldkey), returning the ALPHA minted
    ///      — measured as the getStake delta since the minted amount is
    ///      price-dependent and the chain only tracks the pooled aggregate.
    function _stakeToRegistry(uint256 weiAmount) internal returns (uint256 alphaMinted) {
        uint256 amountRao = weiAmount / WEI_PER_RAO;
        if (amountRao == 0) revert ZeroAmount();

        IStaking staking = IStaking(ISTAKING_ADDRESS);
        uint256 stakeBefore = staking.getStake(registryHotkey, selfColdkey, netuid);
        staking.addStake(registryHotkey, amountRao, netuid);
        uint256 stakeAfter = staking.getStake(registryHotkey, selfColdkey, netuid);

        alphaMinted = stakeAfter - stakeBefore;
        if (alphaMinted == 0) revert NoStakeMinted();
    }

    // ─── Views ────────────────────────────────────────────────────────────────

    /// @notice Repo's normalized emission share, scaled by WEIGHT_PRECISION.
    ///         Returns 0 for unknown repos or an empty registry.
    function emissionShareOf(bytes32 repoId) external view returns (uint256) {
        if (!_repos[repoId].exists || grandTotalStake == 0) return 0;
        return (_repos[repoId].totalStake * WEIGHT_PRECISION) / grandTotalStake;
    }

    /// @notice Deterministic repo id for a "owner/name" string (case-insensitive).
    function repoIdOf(string calldata fullName) external pure returns (bytes32) {
        return keccak256(bytes(_toLower(fullName)));
    }

    function isRegistered(bytes32 repoId) external view returns (bool) {
        return _repos[repoId].exists;
    }

    function getRepo(bytes32 repoId) external view returns (Repo memory) {
        return _repos[repoId];
    }

    function stakeOf(bytes32 repoId, address staker) external view returns (uint256) {
        return _stakes[repoId][staker];
    }

    function listRepos() external view returns (bytes32[] memory) {
        return _repoIds;
    }

    // ─── Name canonicalization ────────────────────────────────────────────────

    /// @dev ASCII-lowercase a string. Repo names are ASCII; canonicalizing makes
    ///      the repoId case-insensitive (GitHub treats names case-insensitively).
    function _toLower(string memory s) internal pure returns (string memory) {
        bytes memory b = bytes(s);
        for (uint256 i = 0; i < b.length; i++) {
            uint8 c = uint8(b[i]);
            if (c >= 0x41 && c <= 0x5A) {
                b[i] = bytes1(c + 32);
            }
        }
        return string(b);
    }

    /// @dev Minimal "owner/name" shape check: exactly one "/", non-empty sides.
    function _isValidName(string memory s) internal pure returns (bool) {
        bytes memory b = bytes(s);
        if (b.length < 3) return false; // shortest valid is "a/b"
        uint256 slashCount;
        uint256 slashIdx;
        for (uint256 i = 0; i < b.length; i++) {
            if (b[i] == "/") {
                slashCount++;
                slashIdx = i;
            }
        }
        if (slashCount != 1) return false;
        if (slashIdx == 0 || slashIdx == b.length - 1) return false;
        return true;
    }

    // solhint-disable-next-line no-empty-blocks
    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}
}
