import { ethers } from "hardhat";

// Exercises the Phase 3 flow against an already-deployed proxy on a live chain
// with REAL ALPHA staking (netuid 1, registry hotkey). Registers two repos and
// adds stake, then reads back weights. Amounts are kept modest because the
// localnet's netuid-1 AMM reserves are small, so the ALPHA minted per TAO is
// price-dependent (assertions are invariant-based, not exact). Requires
// PROXY_ADDRESS. Prints parseable key=value lines for the e2e harness.
async function main() {
  const proxyAddress = process.env.PROXY_ADDRESS;
  if (!proxyAddress) throw new Error("PROXY_ADDRESS env var is required");

  const registry = await ethers.getContractAt("RepoRegistry", proxyAddress);
  const fee = await registry.REGISTRATION_FEE();
  const floor = await registry.MIN_STAKE_FLOOR();

  // Register A and B at the stake floor, then add another floor's worth to A.
  // Sequential awaited txs land in separate blocks (per-block staking rate limit).
  await (await registry.registerRepo("smoke/repo-a", { value: fee + floor })).wait();
  await (await registry.registerRepo("smoke/repo-b", { value: fee + floor })).wait();

  const idA = await registry.repoIdOf("smoke/repo-a");
  const idB = await registry.repoIdOf("smoke/repo-b");
  await (await registry.stake(idA, { value: floor })).wait();

  const totalA = (await registry.getRepo(idA)).totalStake; // ALPHA
  const totalB = (await registry.getRepo(idB)).totalStake;
  const shareA = await registry.emissionShareOf(idA); // 1e18-scaled
  const shareB = await registry.emissionShareOf(idB);

  console.log(`COUNT=${await registry.registeredRepoCount()}`);
  console.log(`GRAND_TOTAL_ALPHA=${await registry.grandTotalStake()}`);
  console.log(`TOTAL_A_ALPHA=${totalA}`);
  console.log(`TOTAL_B_ALPHA=${totalB}`);
  console.log(`SHARE_A=${shareA}`);
  console.log(`SHARE_B=${shareB}`);
  console.log(`SHARE_SUM=${shareA + shareB}`);
  console.log("SMOKE_OK");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
