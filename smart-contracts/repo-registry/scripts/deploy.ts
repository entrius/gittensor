import { ethers, upgrades } from "hardhat";

// Deploys the RepoRegistry UUPS proxy and prints parseable key=value lines so
// the dev-environment e2e harness can capture and assert against them.
//
// initialize() resolves the contract's coldkey via the address-mapping
// precompile (0x80C) on the target chain, so a successful deploy also proves
// that precompile is reachable.
const REGISTRY_HOTKEY = process.env.REGISTRY_HOTKEY ?? "0x" + "11".repeat(32);
const NETUID = BigInt(process.env.REGISTRY_NETUID ?? "1");

async function main() {
  const [deployer] = await ethers.getSigners();

  const Factory = await ethers.getContractFactory("RepoRegistry");
  const proxy = await upgrades.deployProxy(
    Factory,
    [deployer.address, REGISTRY_HOTKEY, NETUID],
    { kind: "uups" }
  );
  await proxy.waitForDeployment();

  const address = await proxy.getAddress();
  const implAddress = await upgrades.erc1967.getImplementationAddress(address);

  console.log(`PROXY_ADDRESS=${address}`);
  console.log(`IMPL_ADDRESS=${implAddress}`);
  console.log(`VERSION=${await proxy.version()}`);
  console.log(`OWNER=${await proxy.owner()}`);
  console.log(`DEPLOYER=${deployer.address}`);
  console.log(`NETUID=${await proxy.netuid()}`);
  console.log(`SELF_COLDKEY=${await proxy.selfColdkey()}`);
  console.log(`REGISTERED_REPO_COUNT=${await proxy.registeredRepoCount()}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
