// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.30;

// Subtensor EVM precompiles used by the registry. Addresses and signatures
// mirror opentensor/subtensor precompiles/src/solidity/{stakingV2,addressMapping}.sol.

address constant ISTAKING_ADDRESS = 0x0000000000000000000000000000000000000805;
address constant IADDRESS_MAPPING_ADDRESS = 0x000000000000000000000000000000000000080C;

/// @dev Staking precompile V2. The precompile uses the *calling contract's*
///      address (mapped to its SS58) as the coldkey — so this contract custodies
///      the ALPHA it stakes. `addStake` amount is in RAO (1e9 = 1 TAO) and is
///      withdrawn from the caller's free balance; `removeStake`/`burnAlpha`
///      amounts are in ALPHA.
interface IStaking {
    function addStake(bytes32 hotkey, uint256 amount, uint256 netuid) external payable;

    function removeStake(bytes32 hotkey, uint256 amount, uint256 netuid) external payable;

    function burnAlpha(bytes32 hotkey, uint256 amount, uint256 netuid) external payable;

    function getStake(
        bytes32 hotkey,
        bytes32 coldkey,
        uint256 netuid
    ) external view returns (uint256);
}

/// @dev Converts an EVM H160 to its Substrate SS58 AccountId (Blake2 hashed
///      mapping). Lets the contract learn its own coldkey on-chain.
interface IAddressMapping {
    function addressMapping(address target) external view returns (bytes32);
}
