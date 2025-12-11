<div align="center">
  <a href="https://gittensor.io/">
    <img src="assets/gt-w-name.jpg" alt="Gittensor Logo" width="100%" />
  </a>
</div>

<h1 align="center">Gittensor</h1>

<div align="center">

  [![Discord Chat](https://img.shields.io/discord/308323056592486420.svg?logo=discord)](https://discord.gg/aK2ZNUQfRt)
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
  [![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/release/python-3110/)
  [![Top Language](https://img.shields.io/github/languages/top/entrius/gittensor)](https://github.com/entrius/gittensor)

  <br />

  **Accelerating Open Source Development through Incentivized Contributions**

  [Website](https://gittensor.io) • [Twitter](https://x.com/gittensor_io) • [Discord](https://discord.gg/aK2ZNUQfRt) • [App](https://app.gittensor.io)
</div>

---

## 📖 Introduction

**Gittensor** is a decentralized subnet on the Bittensor network designed to incentivize meaningful contributions to open-source software (OSS). By bridging the gap between developers and repositories, Gittensor rewards users for submitting valuable Pull Requests (PRs) to recognize repositories.

Whether you are a **Miner** submitting code or a **Validator** verifying contributions, Gittensor provides a transparent and meritocratic platform for OS sustainability.

### 🌟 Key Features
- **Incentivized Coding**: Earn rewards for merged PRs in high-impact repositories.
- **Fair Scoring**: Advanced scoring algorithms account for repository weight, language complexity, and code impact.
- **Automated Validation**: Validators strictly check GitHub Personal Access Tokens (PATs) and PR status.
- **Dynamic Growth**: Collaborative emissions scaling ensures the network grows with its contributors.

---

## 🏗️ Architecture

The Gittensor ecosystem consists of two primary roles:

| Role | Description | Responsibility |
|------|-------------|----------------|
| **⛏️ Miners** | Developers contributing code | Submit PRs to [recognized repositories](https://gittensor.io/repositories) using a registered Github account. |
| **🔍 Validators** | Verifiers of work | query Miners for their GitHub PATs, scan for valid merged PRs, and calculate scores based on contribution value. |

---


## 🚀 Getting Started

Select your role to view the detailed setup and running instructions:

### [⛏️ Miner Guide](gittensor/miner/README.md)
Everything you need to know about setting up your miner, creating your GitHub PAT, and starting the mining process.

### [🔍 Validator Guide](gittensor/validator/README.md)
Instructions for registering a validator, configuring your environment, and running the validation logic.

---

## 📊 Scoring & Rewards

Your score determines your emission rewards. It is calculated based on the quality and impact of your contributions.

### Important Structures

#### Master Repositories & Weights
A list of repositories pulled from GitHub that have been deemed valid for scoring. They each have an associated weight based on factors like: forks, commits, contributors, stars, etc.

> [!NOTE]
> This list is dynamic. It will see various audits, additions, deletions, weight changes, and shuffles as the subnet matures. Recommendations for new open source repositories are welcome; repos are more likely to be included if they provide contributing guidelines, are active/community driven, and provide value.

#### Programming Language Weights
A list of major file types/extensions, mostly related to programming languages, but also including documentation and other common files. Each extension has a weight for scoring.

> [!NOTE]
> This list is also dynamic. Additions and weight changes will occur as the subnet matures.

### Scoring Criteria

#### Valid PR Filtering
There are multiple checks that a PR must pass to be considered valid for scoring:
*   PR is in a `Merged` state.
*   PR is made to a repository in the master repository list.
*   PR is within the lookback window, `MERGED_PR_LOOKBACK_DAYS`.
*   PR is **not** merged by the person who created it (self-merged).
*   PR is merged to the default branch of the repository.
*   PR is merged to a repository **before** it was considered ‘inactive’ in the master repository list.

### The Scoring Formula
A miner's total score ($S_{\text{miner}}$) is the sum of scores from all valid, merged PRs:

$$ S_{\text{miner}} = \sum_{p \in \text{PRs}} S_p $$

Where a single PR score ($S_p$) is:

$$ S_p = w_{\text{repo}} \cdot \beta_{\text{issue}} \cdot \sum_{f \in \text{Files}_p} w_{\text{lang}}(f) \cdot r_f \cdot c_f^{0.75} $$

| Variable | Definition |
| :--- | :--- |
| $w_{\text{repo}}$ | **Repository Weight**: Higher for impactful/incentivized repos. |
| $\beta_{\text{issue}}$ | **Issue Multiplier**: Bonus if the PR resolves a linked issue. |
| $w_{\text{lang}}(f)$ | **Language Weight**: Multiplier based on file extension (e.g., Rust/C++ > Markdown). |
| $c_f$ | **Changes**: Total lines changed (additions + deletions). |
| $c_f^{0.75}$ | **Scaling Factor**: Diminishing returns preventing line-count inflation gaming. |

### Remarks

1.  **Repository incentivization**: Master repositories with higher weight contribute more to final scores.
2.  **Language weighting**: More valuable programming languages (e.g., C++, Rust) receive higher weights.
3.  **Proportional contribution**: Each file's score is normalized by total PR changes.
4.  **Diminishing returns**: The $0.75$ exponent hinders gaming by inflating line counts but still gives score for bigger PRs.
5.  **Issue solve boost**: PRs that solve issues receive a boost multiplier, giving them more value/score.
6.  **Uniqueness boost**: A multiplier that increases score for miners who consistently make PRs across a wider variety of repositories.

For more details read through our various **[scoring](gittensor/validator/evaluation/scoring.py)** and **[rewards](gittensor/validator/evaluation/reward.py)** functions.

### Errors & Penalties

#### Github Account Too Young
We implement a minimum github account age of 180 days to discourage new account spamming/creation. Any PRs submitted from accounts that do not meet minimum age requirements will not be evaluated, and their score will be set to zero. See [here](gittensor/constants.py) for the exact value.

#### Duplicate Miner Penalty
A duplicate miner is defined as a miner whose Github PAT resolves to a Github ID that another miner is using. Effectively, it is multiple miners using the same account. Upon detection, all duplicated miners receive 0 score.

#### Excessive Open PR Spam Penalty
Miners who open PRs excessively will see their score reduced by a penalty multiplier. This multiplier will decrease linearly for every open PR above a certain threshold. See [here](gittensor/constants.py) for the current penalty constants.

### Collaborative Emissions Scaling

Adopted from bitcast (subnet 93), collaborative emissions scaling has the network ‘unlock’ emissions as more miners join and earn a score. Miners will benefit by experiencing individual and cumulative network growth. Two major values affecting the total emissions unlocked rate are:

*   Total lines changed within the last 30 days (`MERGED_PR_LOOKBACK_DAYS`)
*   Total unique repositories contributed to within the last 30 days

As total lines changed and total unique repositories increase, the percentage of total available emissions increases. Whatever is not released is recycled.

The rate of emissions unlocked will be monitored and adjusted as the subnet matures to ensure fair distribution of alpha.

---

## 🤝 Contributing

We welcome contributions to the Gittensor subnet itself! Please check our [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📄 License

This repository is licensed under the [MIT License](LICENSE).
