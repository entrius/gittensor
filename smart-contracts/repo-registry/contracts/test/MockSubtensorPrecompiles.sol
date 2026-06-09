// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.30;

// Test-only mocks of the subtensor precompiles. In unit tests their runtime
// code is installed at the real precompile addresses (0x805 / 0x80C) via
// hardhat_setCode, so RepoRegistry exercises the exact same call paths it will
// on-chain. The mock mints ALPHA 1:1 with the RAO staked for deterministic
// assertions; the real precompile mints a price-dependent amount.

contract MockStaking {
    // staked[hotkey][coldkey][netuid] = alpha
    mapping(bytes32 => mapping(bytes32 => mapping(uint256 => uint256))) public staked;

    function _coldkeyOf(address account) internal pure returns (bytes32) {
        return bytes32(uint256(uint160(account)));
    }

    function addStake(bytes32 hotkey, uint256 amount, uint256 netuid) external payable {
        staked[hotkey][_coldkeyOf(msg.sender)][netuid] += amount; // 1:1 alpha
    }

    function removeStake(bytes32 hotkey, uint256 amount, uint256 netuid) external payable {
        staked[hotkey][_coldkeyOf(msg.sender)][netuid] -= amount;
    }

    function burnAlpha(bytes32 hotkey, uint256 amount, uint256 netuid) external payable {
        staked[hotkey][_coldkeyOf(msg.sender)][netuid] -= amount;
    }

    function getStake(
        bytes32 hotkey,
        bytes32 coldkey,
        uint256 netuid
    ) external view returns (uint256) {
        return staked[hotkey][coldkey][netuid];
    }
}

contract MockAddressMapping {
    // Same derivation MockStaking uses, so coldkeys line up in tests.
    function addressMapping(address target) external pure returns (bytes32) {
        return bytes32(uint256(uint160(target)));
    }
}
