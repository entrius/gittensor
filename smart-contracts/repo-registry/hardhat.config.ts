import { HardhatUserConfig } from "hardhat/config";
import "@nomicfoundation/hardhat-ethers";
import "@nomicfoundation/hardhat-chai-matchers";
import "@openzeppelin/hardhat-upgrades";

// Local subtensor dev account #0 (well-known Hardhat key). It holds no balance
// by default — the dev-environment funds it from //Alice before deploy:
//   gt-utils/dev-environment/repo-registry/scripts/fund_evm_deployer.py
const DEPLOYER_PK =
  process.env.DEPLOYER_PRIVATE_KEY ??
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";

const config: HardhatUserConfig = {
  solidity: {
    version: "0.8.30",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      // Subtensor EVM is Frontier-based; pin to paris to avoid emitting PUSH0
      // (shanghai), which the chain may not support.
      evmVersion: "paris",
    },
  },
  networks: {
    // Local subtensor node's unified RPC also serves the eth_* namespace.
    subtensorLocal: {
      url: process.env.EVM_RPC_URL ?? "http://127.0.0.1:9944",
      chainId: 42,
      accounts: [DEPLOYER_PK],
    },
  },
};

export default config;
