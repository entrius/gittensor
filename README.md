<p align="center">
  <a href="https://gittensor.io/">
    <img src="assets/gt-logo.jpg" alt="Gittensor Logo" width="800" />
  </a>
</p>

# Gittensor

Incentivize open source contributions.

[![Website](https://img.shields.io/badge/Website-gittensor.io-blue)](https://gittensor.io)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.com/invite/bittensor)
[![Twitter](https://img.shields.io/twitter/follow/gittensor_io?style=social)](https://x.com/gittensor_io)

## Introduction

[Gittensor](https://gittensor.io/) is a [Bittensor subnet](https://docs.learnbittensor.org/subnets/understanding-subnets) (SN74) that accelerates open source software development by rewarding meaningful contributions. Miners earn TAO by making real, merged pull requests to recognized open source repositories.

## How it Works

Miners register with a fine-grained [GitHub personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) (PAT) and contribute to whitelisted open source repositories. When their pull requests get merged, validators authenticate account ownership via the PAT, verify the merged contributions, and score them based on code quality, repository allocation, and programming language factors. Rewards are distributed within each repository's configured emission share.

## Why Gittensor

Open source powers the modern world, yet most contributors work for free. Gittensor solves this by creating a decentralized marketplace where:

- **Real work gets rewarded** — Only merged PRs to legitimate repositories earn emissions
- **Quality over quantity** — Semantic code analysis evaluates actual contribution value
- **Sybil-resistant** — GitHub account verification and merge requirements prevent gaming

The result: a sustainable incentive layer that channels resources toward building and maintaining the software we all depend on.

---

## Miners

No miner neuron required — just register your GitHub PAT with validators using the CLI.

```bash
# Install
git clone https://github.com/entrius/gittensor.git
cd gittensor
uv sync

# Set your GitHub PAT
export GITTENSOR_MINER_PAT=ghp_your_token_here

# Broadcast PAT to validators
gitt miner post --wallet <name> --hotkey <hotkey>

# Check which validators have your PAT stored
gitt miner check --wallet <name> --hotkey <hotkey>
```

See full guide **[here](https://docs.gittensor.io/miner.html)**

## Validators

**Recommended: Deploy with Docker and Docker Watchtower for automatic updates**

```bash
# Quick start
git clone https://github.com/entrius/gittensor.git
cd gittensor
cp .env.example .env
# Edit .env with proper values
nano .env

docker-compose -f docker-compose.vali.yml up -d
```

See full guide **[here](https://docs.gittensor.io/validator.html)**

### Repository Weight Consensus

Repository emission shares are voted by validators, not hardcoded. Each validator publishes a basket of up to **10 repos** to the chain; the **stake-weighted mean** of all eligible baskets becomes the emission shares every validator scores with — computed at a fixed snapshot block so all validators derive byte-identical weights and vtrust is unaffected.

```
1. VOTE      gitt validator weights set            (any GitHub repo, ≤10, weights relative)
                │ set_commitment — hotkey-signed, free, fully on-chain (≤512 B)
                ▼
2. CHAIN     Commitments pallet: netuid → hotkey → basket payload
                │ read at snapshot B = block − (block % 3600)   ~2×/day
                ▼
3. AGGREGATE every validator, deterministically:
             filter vpermit ∧ stake ≥ 30k α → normalize each basket → stake-weighted mean
             gates: <50% eligible stake voted → baked JSON; errors → last-good → baked JSON
                │ same block ⇒ same bytes ⇒ same aggregate
                ▼
4. SCORING   emission_share ⟵ aggregate (known repo → tuned params, novel → defaults)
             → identical weight vectors across validators → full vtrust
```

Rules and mechanics:
- Any GitHub repository is votable — no whitelist. A repo only *scores* once the GT mirror tracks it (owner installs the Gittensor GitHub App + registration); until then its share redistributes to active repos.
- Weights are relative within your basket; the aggregator normalizes every basket to 1.0, so influence is exactly proportional to stake.
- Votes take effect at the next snapshot (~12h cadence). No basket published → loud warning, the validator still scores using the aggregate.
- Per-repo scoring hyperparameters (label multipliers, maintainer cut, eligibility) come from `master_repositories.json` when present, code defaults otherwise.
- Recommended: connect to an archive endpoint (`wss://archive.chain.opentensor.ai:443`) so snapshot state is always queryable; lite nodes gracefully fall back to the last-good aggregate.

CLI:

```bash
gitt validator weights show               # aggregated shares, voter turnout, gate status
gitt validator weights set                # interactively build + publish your basket
gitt validator weights basket <hotkey>    # inspect any validator's basket
```

## Reward Algorithm

### Important Structures

- Master Repositories & Emission Shares

A list of repositories pulled from GitHub that have been deemed valid for scoring. They each have a configured emission share that bounds how much of the scoring pool the repository can receive in a round.

_NOTE: this list will be dynamic. It will see various audits, additions, deletions, emission-share changes, and shuffles as the subnet matures._

_NOTE: don’t be afraid to provide recommendations for your favorite open source repositories and the team will review it as a possible addition. A repo is more likely to be included if: they provide contributing guidelines, are active/community driven, provide value/have users_

- Programming Language Weights

A list of major file types/extensions, mostly related to programming languages, but also plenty of markdown, documentation, and other common files are included. Each extension has a weight for scoring. If the extension has a language full name then code in those languages will be evaluated using token-based scoring.

_NOTE: this list will also be dynamic. Additions, and weight changes will occur as the subnet matures._

- Token Weights

Weights assigned to AST (Abstract Syntax Tree) node types for token-based scoring, including structural elements (functions, classes) and leaf tokens (identifiers, literals), enabling semantic evaluation of code changes.

### Scoring

See full guide **[here](https://docs.gittensor.io/oss-contributions.html)**

## License

MIT - See [LICENSE](LICENSE) for details.
