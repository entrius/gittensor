import { expect } from "chai";
import { ethers, upgrades, network } from "hardhat";
import { anyUint } from "@nomicfoundation/hardhat-chai-matchers/withArgs";

const ONE = 10n ** 18n; // 1 TAO in native EVM value (wei)
const RAO = 10n ** 9n; // 1 TAO in substrate units; mock mints 1 ALPHA per RAO
const FEE = ONE; // REGISTRATION_FEE = 1 ether
const FLOOR = 10n * ONE; // MIN_STAKE_FLOOR = 10 ether
const HOTKEY = "0x" + "11".repeat(32); // dummy registry hotkey
const NETUID = 1n;

const STAKING = "0x0000000000000000000000000000000000000805";
const ADDR_MAP = "0x000000000000000000000000000000000000080C";

// ALPHA the mock mints for `stakeWei` of committed TAO (1 alpha per rao).
const toAlpha = (stakeWei: bigint) => stakeWei / RAO;

describe("RepoRegistry", () => {
  // Install mock precompile bytecode at the real precompile addresses so the
  // contract hits the same call paths it will on-chain.
  async function installPrecompileMocks() {
    const Staking = await ethers.getContractFactory("MockStaking");
    const staking = await Staking.deploy();
    await staking.waitForDeployment();
    const AddrMap = await ethers.getContractFactory("MockAddressMapping");
    const addrMap = await AddrMap.deploy();
    await addrMap.waitForDeployment();

    await network.provider.send("hardhat_setCode", [
      STAKING,
      await ethers.provider.getCode(staking.target),
    ]);
    await network.provider.send("hardhat_setCode", [
      ADDR_MAP,
      await ethers.provider.getCode(addrMap.target),
    ]);
  }

  async function deploy() {
    await installPrecompileMocks();
    const [owner, alice, bob] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("RepoRegistry");
    const registry = await upgrades.deployProxy(Factory, [owner.address, HOTKEY, NETUID], {
      kind: "uups",
    });
    await registry.waitForDeployment();
    return { registry, owner, alice, bob };
  }

  // value to send for an initial registration committing `stakeWei` of stake.
  const reg = (stakeWei: bigint) => ({ value: FEE + stakeWei });

  describe("Phase 0 — proxy & ownership", () => {
    it("deploys behind a UUPS proxy and initializes state", async () => {
      const { registry } = await deploy();
      expect(await registry.version()).to.equal("0.3.0");
      expect(await registry.registeredRepoCount()).to.equal(0n);
      expect(await registry.grandTotalStake()).to.equal(0n);
    });

    it("sets the initial owner and subnet config", async () => {
      const { registry, owner } = await deploy();
      expect(await registry.owner()).to.equal(owner.address);
      expect(await registry.registryHotkey()).to.equal(HOTKEY);
      expect(await registry.netuid()).to.equal(NETUID);
    });

    it("caches its own coldkey from the address-mapping precompile", async () => {
      const { registry } = await deploy();
      const expected = ethers.zeroPadValue(
        (await registry.getAddress()).toLowerCase(),
        32
      );
      expect(await registry.selfColdkey()).to.equal(expected);
    });

    it("cannot be re-initialized", async () => {
      const { registry, alice } = await deploy();
      await expect(
        registry.initialize(alice.address, HOTKEY, NETUID)
      ).to.be.revertedWithCustomError(registry, "InvalidInitialization");
    });

    it("transfers ownership in two steps (Ownable2Step)", async () => {
      const { registry, owner, alice } = await deploy();
      await registry.connect(owner).transferOwnership(alice.address);
      expect(await registry.owner()).to.equal(owner.address);
      expect(await registry.pendingOwner()).to.equal(alice.address);
      await registry.connect(alice).acceptOwnership();
      expect(await registry.owner()).to.equal(alice.address);
    });

    it("only the owner can authorize an upgrade", async () => {
      const { registry, alice } = await deploy();
      await expect(
        registry.connect(alice).upgradeToAndCall(ethers.ZeroAddress, "0x")
      ).to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount");
    });
  });

  describe("Phase 1/3 — registration", () => {
    it("registers a repo, staking ALPHA and recording fee, block, immunity", async () => {
      const { registry, owner } = await deploy();
      const id = await registry.repoIdOf("octo/cat");
      const expectedAlpha = toAlpha(30n * ONE);

      await expect(registry.registerRepo("octo/cat", reg(30n * ONE)))
        .to.emit(registry, "RepoRegistered")
        .withArgs(id, owner.address, "octo/cat", expectedAlpha, anyUint);

      const repo = await registry.getRepo(id);
      expect(repo.owner).to.equal(owner.address);
      expect(repo.fullName).to.equal("octo/cat");
      expect(repo.totalStake).to.equal(expectedAlpha);
      expect(repo.exists).to.equal(true);
      expect(repo.immunityUntil - repo.registeredAtBlock).to.equal(7200n);

      expect(await registry.registeredRepoCount()).to.equal(1n);
      expect(await registry.grandTotalStake()).to.equal(expectedAlpha);
      expect(await registry.collectedFees()).to.equal(FEE);
      expect(await registry.stakeOf(id, owner.address)).to.equal(expectedAlpha);
    });

    it("canonicalizes case so Foo/Bar == foo/bar (anti-dup)", async () => {
      const { registry } = await deploy();
      await registry.registerRepo("Octo/Cat", reg(30n * ONE));
      const id = await registry.repoIdOf("octo/cat");
      expect(await registry.isRegistered(id)).to.equal(true);
      expect((await registry.getRepo(id)).fullName).to.equal("octo/cat");
      await expect(
        registry.registerRepo("OCTO/CAT", reg(30n * ONE))
      ).to.be.revertedWithCustomError(registry, "AlreadyRegistered");
    });

    it("rejects malformed names", async () => {
      const { registry } = await deploy();
      for (const bad of ["", "noslash", "/name", "owner/", "a/b/c"]) {
        await expect(
          registry.registerRepo(bad, reg(30n * ONE))
        ).to.be.revertedWithCustomError(registry, "InvalidName");
      }
    });

    it("rejects payment below fee + floor", async () => {
      const { registry } = await deploy();
      await expect(
        registry.registerRepo("octo/cat", { value: FEE + FLOOR - 1n })
      ).to.be.revertedWithCustomError(registry, "InsufficientPayment");
    });

    it("rejects duplicate registration", async () => {
      const { registry } = await deploy();
      await registry.registerRepo("octo/cat", reg(30n * ONE));
      await expect(
        registry.registerRepo("octo/cat", reg(30n * ONE))
      ).to.be.revertedWithCustomError(registry, "AlreadyRegistered");
    });
  });

  describe("Phase 1/3 — staking & weight", () => {
    it("accumulates multi-staker ALPHA", async () => {
      const { registry, owner, alice, bob } = await deploy();
      const id = await registry.repoIdOf("octo/cat");
      await registry.registerRepo("octo/cat", reg(30n * ONE));

      await expect(registry.connect(alice).stake(id, { value: 20n * ONE }))
        .to.emit(registry, "Staked")
        .withArgs(id, alice.address, toAlpha(20n * ONE), toAlpha(50n * ONE));
      await registry.connect(bob).stake(id, { value: 50n * ONE });

      expect((await registry.getRepo(id)).totalStake).to.equal(toAlpha(100n * ONE));
      expect(await registry.stakeOf(id, owner.address)).to.equal(toAlpha(30n * ONE));
      expect(await registry.stakeOf(id, alice.address)).to.equal(toAlpha(20n * ONE));
      expect(await registry.stakeOf(id, bob.address)).to.equal(toAlpha(50n * ONE));
      expect(await registry.grandTotalStake()).to.equal(toAlpha(100n * ONE));
    });

    it("reverts staking to an unknown repo or with zero value", async () => {
      const { registry, alice } = await deploy();
      const id = await registry.repoIdOf("octo/cat");
      await expect(
        registry.connect(alice).stake(id, { value: ONE })
      ).to.be.revertedWithCustomError(registry, "NotRegistered");

      await registry.registerRepo("octo/cat", reg(30n * ONE));
      await expect(
        registry.connect(alice).stake(id, { value: 0n })
      ).to.be.revertedWithCustomError(registry, "ZeroAmount");
    });

    it("computes normalized emission share across repos", async () => {
      const { registry } = await deploy();
      const a = await registry.repoIdOf("org/a");
      const b = await registry.repoIdOf("org/b");
      await registry.registerRepo("org/a", reg(60n * ONE));
      await registry.registerRepo("org/b", reg(40n * ONE));

      expect(await registry.emissionShareOf(a)).to.equal((60n * ONE) / 100n);
      expect(await registry.emissionShareOf(b)).to.equal((40n * ONE) / 100n);
      const sum =
        (await registry.emissionShareOf(a)) +
        (await registry.emissionShareOf(b));
      expect(sum).to.equal(ONE);
    });

    it("returns zero share for unknown repos", async () => {
      const { registry } = await deploy();
      expect(
        await registry.emissionShareOf(await registry.repoIdOf("no/pe"))
      ).to.equal(0n);
    });
  });
});
