<p align="center">
  <a href="https://gittensor.io/">
    <img src="assets/gt-logo.jpg" alt="Gittensor Logo" width="800" />
  </a>
</p>

# Gittensor

Incentivize open source contributions.

[![Website](https://img.shields.io/badge/Website-gittensor.io-blue?logo=googlechrome&logoColor=white)](https://gittensor.io)
[![Discord](https://img.shields.io/discord/1140657726694240287?label=Discord&logo=discord)](https://discord.com/invite/bittensor)
[![Twitter](https://img.shields.io/twitter/follow/gittensor_io?style=social)](https://x.com/gittensor_io)

## Introduction

Gittensor is a Bittensor subnet (SN74) that accelerates open source software development by rewarding meaningful contributions. Miners earn TAO by making real, merged pull requests to recognized open source repositories.

## How it Works

Miners register with a fine-grained GitHub personal access token (PAT) and contribute to whitelisted open source repositories. When their pull requests get merged, validators authenticate account ownership via the PAT, verify the merged contributions, and score them based on code quality, repository weight, and programming language factors. Rewards are distributed proportionally to contribution scores.

## Why Gittensor

Open source powers the modern world, yet most contributors work for free. Gittensor solves this by creating a decentralized marketplace where:

- **Real work gets rewarded** — Only merged PRs to legitimate repositories earn emissions
- **Quality over quantity** — Semantic code analysis evaluates actual contribution value
- **Sybil-resistant** — GitHub account verification and merge requirements prevent gaming

The result: a sustainable incentive layer that channels resources toward building and maintaining the software we all depend on.

---

## Miners

**Recommended: Deploy with Docker**

```bash
# Quick start
git clone https://github.com/entrius/gittensor.git
cd gittensor
cp env.example .env
# Edit .env with proper values
nano .env

docker-compose -f docker-compose.miner.yml up -d
```

See full guide **[here](https://docs.gittensor.io/miner.html)**

## Validators

**Recommended: Deploy with Docker and Docker Watchtower for automatic updates**

```bash
# Quick start
git clone https://github.com/entrius/gittensor.git
cd gittensor
cp env.example .env
# Edit .env with proper values
nano .env

docker-compose -f docker-compose.vali.yml up -d
```

See full guide **[here](https://docs.gittensor.io/validator.html)**

## Reward Algorithm

### Important Structures

- Master Repositories & Weights

A list of repositories pulled from github that have been deemed valid for scoring. They each have an associated weight based on factors like: forks, commits, contributors, stars, etc.

_NOTE: this list will be dynamic. It will see various audits, additions, deletions, weight changes, and shuffles as the subnet matures._

_NOTE: don’t be afraid to provide recommendations for your favorite open source repositories and the team will review it as a possible addition. A repo is more likely to be included if: they provide contributing guidelines, are active/community driven, provide value/have users_

- Programming Language Weights

A list of major file types/extensions, mostly related to programming languages, but also plenty of markdown, documentation, and other common files are included. Each extension has a weight for scoring. If the extension has a language full name then it code in those languages will be evaluated using token-based scoring.

_NOTE: this list will also be dynamic. Additions, and weight changes will occur as the subnet matures._

- Token Weights

Weights assigned to AST (Abstract Syntax Tree) node types for token-based scoring, including structural elements (functions, classes) and leaf tokens (identifiers, literals), enabling semantic evaluation of code changes.

### Scoring

See full guide **[here](https://docs.gittensor.io/scoring.html)**
