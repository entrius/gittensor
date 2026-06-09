# repo-registry

On-chain repository registry for the Gittensor subnet — a Solidity/EVM contract
on Subtensor that replaces the static `master_repositories.json`. GitHub repo
owners register on-chain, commit ALPHA stake (multi-staker), and normalized
stake drives each repo's emission share. Scoring hyperparameters live off-chain
in gittensor-db; only stake/ownership/lifecycle is on-chain.

Full design: [`REPO_REGISTRY_DESIGN.md`](../../REPO_REGISTRY_DESIGN.md) at the repo root.

> **Note:** Like the rest of `smart-contracts/`, this is maintainer-only and not
> accepting external contributions (see `CONTRIBUTING.md`).

## Stack

- **Solidity 0.8.30**, `evmVersion: paris` (Subtensor EVM is Frontier-based).
- **UUPS upgradeable** + `Ownable2StepUpgradeable` (OpenZeppelin v5).
- **Hardhat** + `@openzeppelin/hardhat-upgrades`.

## Commands

```bash
npm install
npm run build          # compile
npm test               # in-memory unit tests (no chain needed)
npm run deploy:local   # deploy UUPS proxy to local subtensor (needs funded deployer)
```

The local deploy targets `http://127.0.0.1:9944` (chainId 42) and uses the
well-known Hardhat dev key, which must be funded with TAO first. The
dev-environment harness handles bring-up + funding + teardown:

```bash
cd ../../../gt-utils/dev-environment/repo-registry && ./e2e.sh
```

## Status

Phase 0: UUPS skeleton (deploy + owner + `version()`). Registration, staking,
burn, and eviction land in later phases per the design doc.
