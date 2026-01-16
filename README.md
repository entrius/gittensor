<p align="center">
  <a href="https://gittensor.io/">
    <img src="assets/gt-logo.jpg" alt="Gittensor Logo" width="800" />
  </a>
</p>

# Gittensor

Gittensor aims to accelerate the development of open source software (OSS) and enable OSS developers to be rewarded for meaningful work. We incentivize open source contributions.

## Socials

- **Website:** [gittensor.io](https://gittensor.io)
- **X (Twitter):** [@gittensor_io](https://x.com/gittensor_io)
- **Discord:** [Join our channel, 74, in the Bittensor discord](https://discord.gg/aK2ZNUQfRt)

---

## At a Glance

- **Miners**: Provide a fine-grained Github personal access token (PAT) and create pull requests (PRs) to recognized repositories.
- **Validators**: Utilize miner PATs to authenticate account ownership and search recognized repositories for successfully merged miner PRs

---

## Miners

See full guide **[here](https://docs.gittensor.io/miner.html)**

## Validators

**Recommended: Deploy with Docker for automatic updates**

```bash
# Quick start
git clone https://github.com/entrius/gittensor.git
cd gittensor
cp env.example .env
# Edit .env with your wallet details and WANDB_API_KEY
docker-compose up -d
```

Your validator will automatically update within 5 minutes of any push to main.

See full guide **[here](https://docs.gittensor.io/validator.html)** | [Docker deployment docs](docs/DOCKER.md)

## Scoring & Rewards

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

### Collaborative emissions scaling

Adopted from bitcast, subnet 93, collaborative emissions scaling has the network ‘unlock’ emissions as more miners join and earn a score. Miners will benefit by experiencing individual and cumulative network growth. Two major values affecting the total emissions unlocked rate are:

- Total lines changed within the last 90 days (`PR_LOOKBACK_DAYS`)
- Total merged pull requests within the last 90 days
- Total unique repositories contributed to within the last 90 days

As total lines changed and total unique repositories increases, the percentage of total available emissions increases. Whatever is not released is recycled.

The rate of emissions unlocked will be monitored and adjusted as the subnet matures to ensure fair distribution of alpha.
