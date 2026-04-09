# Issue Discovery Rewards

## Overview

A new reward mechanism where miners earn score for discovering issues in tracked repositories. The scoring model mirrors the existing merged PR model — issues that lead to merged PRs are treated as successes, while issues closed without resolution count against the discoverer.

Issue discovery is purely additive — PRs are scored exactly as today whether or not they have a linked issue. A PR with no linked issue just means nobody earns a discovery score. The solver's score is unaffected.

Issue discovery only applies when the issue creator is a registered miner. Non-miner issue creators don't participate — the PR solver gets normal scoring and nothing happens on the discovery side.

---

## Credibility

Issue discoverers build a separate `issue_credibility` score, stored independently from PR credibility (`credibility`):

```
issue_credibility = solved_issues / (solved_issues + closed_issues)
```

- **Solved issue**: an issue that was resolved via a merged PR.
- **Closed issue**: an issue closed without an attached merged PR — counts against credibility.
- **Transferred issue**: any issue that has been transferred at any point (detected via GitHub timeline API `TransferredEvent`) is treated as closed/failed — 0 discovery score and counts against credibility. No exceptions regardless of destination. Prevents exploit where discoverers transfer closed issues to burner repos to dodge credibility hits.

Issue credibility is **computed fresh every scoring round** (stateless), same as PR credibility. No persistent state needed — count solved and closed issues within the lookback window each cycle.

### Qualification Gates

Stricter than OSS contributions to increase the runway required for eligibility and raise the cost of Sybil attacks:

1. **Minimum 7 valid solved issues** — a "valid" solved issue is one where the solving PR has `token_score >= 5` (`MIN_TOKEN_SCORE_FOR_BASE_SCORE`).
2. **Minimum 80% issue credibility** (`MIN_ISSUE_CREDIBILITY = 0.80`).
3. **Credibility mulligan** — `CREDIBILITY_MULLIGAN_COUNT = 1`, mirroring OSS contributions.

Eligibility is evaluated globally across all repos (not per-repo), same as OSS contributions.

---

## Scoring

### Base Score

The issue discovery base score equals the base score of the PR that solved the issue. The discoverer's reward scales with the actual value of the work their issue generated.

The quality signal for an issue is the resulting PR's token score. PRs with `token_score < 5` receive 0 base score (per `MIN_TOKEN_SCORE_FOR_BASE_SCORE` in `constants.py`), so trivial issues that generate trivial PRs yield near-zero discovery score. The discoverer still gets a small credibility bump from a solved issue — this is acceptable.

### Contribution Bonus

The contribution bonus is per-PR (based on `scoring_result.total_score`), not a miner-level historical metric. It passes through to the discoverer because it naturally scales with PR quality — it reflects the value of the work generated. The threshold (`CONTRIBUTION_SCORE_FOR_FULL_BONUS = 2000`) is high enough that farming is impractical. Can always lower `MAX_CONTRIBUTION_BONUS` if needed.

### Same-Account Double Dipping

When the same GitHub account is both the issue author and the PR author: **zero issue discovery score**, but the solved issue **still counts for issue credibility**.

Rationale:
- Discovery rewards are for finding problems *others* solve.
- No reason to avoid linking your own issues (credibility still benefits).
- Alt-account gaming is acknowledged but bounded by independent credibility gates on each account.
- Can add heuristic detection later (timing patterns, always-same-solver, etc.) if needed.

### Review Quality Multiplier (Cliff Model)

Both solver and discoverer are penalized equally when changes are requested. Clean-PR bonus of `1.1` when zero `CHANGES_REQUESTED` rounds. Cliff model — first review round drops from 1.1 to 1.0, then subtracts 0.15 per round (linear after the cliff).

- **Both solver and discoverer:** `1.1` clean bonus, then `1.0 - 0.15n` once changes are requested (n = number of `CHANGES_REQUESTED` rounds)

| Rounds | Multiplier |
|--------|------------|
| 0      | 1.10       |
| 1      | 0.85       |
| 2      | 0.70       |
| 3      | 0.55       |

Rationale:
- Same penalty for both sides — no need to differentiate since same-account double dipping is blocked (see above).
- The 1.1 clean bonus rewards "perfect PRs out of the box" — the desired behavior.
- The cliff from 1.1 → penalty makes the first `CHANGES_REQUESTED` round sting (~23% swing).
- Maintainers already reserve `CHANGES_REQUESTED` for meaningful problems (minor stuff gets comments, no penalty).

### Shared Multipliers

The following multipliers from PR scoring carry over to issue discovery:

- **repo_weight_multiplier** — yes
- **time_decay_multiplier** — yes, anchored to solving PR's merge date (same as lookback window)
- **credibility_multiplier** — uses `issue_credibility`, not PR credibility
- **open_issue_spam_multiplier** — issue-specific threshold (see Spam Control below), replaces `open_pr_spam_multiplier`

The following do **not** carry over:

- **open_pr_spam_multiplier** — replaced by `open_issue_spam_multiplier`
- **pioneer_dividend** — does not exist for issue discovery (out of scope for this feature)

---

## Issue-to-PR Linking

### How issues are linked to solving PRs

Uses the same mechanism as the existing issue bounty/competition system. GitHub's GraphQL API provides the `closingIssuesReferences` field, which natively resolves `fixes #N` / `closes #N` / `resolves #N` keywords into structured cross-reference events. The validator queries issue timeline items (`CROSS_REFERENCED_EVENT`) and validates:

1. The PR's `baseRepository` matches the issue's repo.
2. The PR state is `MERGED`.
3. The issue number appears in the PR's `closingIssuesReferences`.

See existing implementation: `gittensor/utils/github_api_tools.py` — `_search_issue_referencing_prs_graphql()` and `find_solver_from_cross_references()`.

### One PR per issue

Only the **most recent merged PR** that solved the issue counts (latest `mergedAt`), consistent with the existing issue competition logic (`github_api_tools.py:1107-1115`). If multiple PRs claim to solve the same issue, the latest merge wins. All others are ignored for issue discovery purposes.

### One issue credited per PR

When a single PR references multiple issues (`fixes #10, fixes #11, fixes #12`), only **one** issue discoverer receives the discovery score. The remaining issue creators still get credibility credit (solved/merged) but no score.

Selection heuristic: **TBD** (earliest-created is the likely default). Options:
- Credit the **earliest-created issue** (rewards the first discoverer).
- Credit the issue with the **most engagement** (comments, reactions).
- Split the score across discoverers.

### Retroactive Linking

If a PR is scored in cycle N but the issue link is established in cycle N+1 (e.g., maintainer links the issue after merge), the issue discovery score is awarded when the link is detected. We use whatever the API returns at scoring time.

---

## Scoring Pipeline

### Execution order

1. Score PRs (existing pipeline — base score, multipliers, credibility).
2. Score issues using the solving PR's base score.
3. Apply issue-specific multipliers (review quality cliff model, issue bonus, repo weight, time decay, credibility).
4. Compute issue credibility.
5. Produce issue discovery scores.

Issue discovery scores do not feed back into PR scoring.

### Lookback Window

Issue discovery uses the same lookback window as PRs (~35 days per roadmap). The window is anchored to the **solving PR's merge date**, not the issue creation date.

> **Note:** This means an issue created 90 days ago but solved today is within the window. An issue created 30 days ago whose solving PR was merged 40 days ago is outside the window.

### Weight / Pool Separation

Issue discovery scores are split into their own pool in the weight vector, same approach as issue competitions and merge predictions. The validator manually splits the weight allocation in code — no chain-level changes needed.

---

## Anti-Gaming

### Post-PR Edit Protection

If an issue is edited at any point after the solving PR's **`merged_at`** timestamp:

- The issue receives **0 score**.
- The issue **counts as closed** (hurts credibility).

Anchored to `merged_at` (not `created_at`) so discoverers can add clarifying context while a PR is in review without being penalized.

**Edit detection (current):** Uses `updated_at` as a rough proxy. Acknowledged that `updated_at` fires on bot activity, comments, labels, etc. — accept false positives for now.

**Edit detection (future):** Upgrade to timeline/events API for body-only edit detection in a later update.

### Timing / Sniping Protection

If a miner files an issue for work already in progress (a PR is opened shortly after the issue), maintainers can close the issue as invalid. A closed-without-PR issue hurts the sniper's credibility. Maintainers can cross-reference timings between issue creation and PR creation to identify suspicious patterns.

No automated minimum time gap is enforced — this is left to maintainer judgment to avoid penalizing legitimate fast turnaround.

### Maintainer Influence

Maintainers have power over issue lifecycle (closing issues, linking PRs). This is by design — the same trust model applies to PRs (maintainers decide what gets merged). The Gittensor team currently curates the repository list, which limits exposure to adversarial maintainers. As repository selection opens up to miners, maintainer trust becomes a larger concern and may need additional safeguards.

### Trivial Issue Farming

Filing trivial issues on active repos to farm credibility is mitigated by the token score threshold: PRs with `token_score < 5` get 0 base score, so the resulting issue discovery score is near-zero. The discoverer gains minor credibility, which is acceptable — the qualification gates and credibility thresholds prevent this from scaling into meaningful emissions.

### Issue Deletion

GitHub does not allow regular users to delete issues — only repo admins can, and it's a destructive action. If an admin deletes an issue, we lose tracking (both positive and negative credibility impact). This is a non-concern in practice.

### Forked Repo Issues

Issues filed on forks of tracked repos are ignored entirely — no score (positive or negative) and no credibility impact. Only issues on the actual tracked repository count. This is enforced naturally by the existing linking mechanism: `closingIssuesReferences` and the repo-centric closed scan both operate on the tracked repo, not its forks.

### Issue Transfers

Any issue that has been transferred at any point is treated as **closed/failed** — 0 discovery score and counts against credibility. No exceptions regardless of destination. Detection via GitHub timeline API `TransferredEvent`. Prevents exploit where discoverers transfer closed issues to burner repos to dodge credibility hits.

### State Transitions (Close → Reopen → Solve)

Whatever state the API returns at scoring time is what counts. If an issue is closed (credibility hit in cycle N), then reopened and solved (credibility positive in cycle N+1), both events are reflected in their respective scoring cycles. No smoothing or retroactive correction.

### Spam Control via Open Thresholds

There is no collateral requirement for issues. Spam is controlled through open issue thresholds:

- **Base threshold**: 5 open issues (half the PR base of 10).
- **Dynamic scaling**: +1 allowed open issue per 300 merged token score from solved issues.
- **Exceeding the threshold**: 0 score for all issues (binary, same as OSS contributions).

---

## Emissions — Hardcoded Per Competition

Dynamic emissions are being removed. Each competition gets a fixed percentage of total emissions, hardcoded in the validator. This replaces the exponential unlock curve (`dynamic_emissions.py`) that scaled rewards based on network-wide unique repos and token score.

**Rationale:** Dynamic emissions added complexity without proportional benefit. Hardcoded splits are easier to reason about, tune, and audit. Adjustments happen via code changes with PR review, not opaque curves.

**Emission split:**

| Competition | Share | Notes |
|---|---|---|
| OSS Contributions (PRs) | 30% | Shipping code |
| Issue Discovery | 30% | Finding problems others solve |
| Issue Competitions (Treasury) | 15% | Funds bounties via smart contract (UID 111) |
| Unallocated (burn/recycle) | 25% | Recycles to UID 0 |

The unallocated 25% recycles to UID 0, same mechanism as today. Each pool normalizes scores independently — a miner's share within a pool is based on their score relative to other participants in that pool.

**Early participation windfall:** If few miners participate in issue discovery early, they split the entire pool — potentially outsized rewards. This is intentional: miners who keep up with codebase updates and act early are rewarded for being first movers. No participation floor is needed.

---

## GitHub API / Data Collection Strategy

### What We Have vs What We Need

The existing PR scoring pipeline fetches each miner's PRs via GraphQL, which includes `closingIssuesReferences(first: 3)` on every PR. This gives us issue metadata (author, state, dates) for issues linked to miner PRs — but only those issues.

| Case | Effect | How to detect | Have it today? | API cost |
|---|---|---|---|---|
| **Solved by miner PR** | ✅ Positive credibility + discovery score | Miner B's merged PR has `closingIssuesReferences` → check if issue author is miner A | **Yes** | 0 extra calls |
| **Solved by non-miner PR** | ✅ Positive credibility (no score — solver not a miner) | A non-miner's merged PR solved miner A's issue. We never fetch non-miner PRs, so we're blind to this. | **No** | Timeline call per issue to find solver |
| **Closed without any PR** | ❌ Negative credibility | Miner's issue closed as wontfix/duplicate/invalid. No PR linkage exists, so invisible to current pipeline. | **No** | Part of repo scan |
| **Open issues on tracked repos** | Spam threshold (0 score if over threshold) | Need count of open issues per miner, scoped to tracked repos only. | **No** | See options below |

**Key constraint:** Cases 2 and 3 are found by the same repo-centric scan, but we can't distinguish them without a timeline fallback call per issue. Case 4 needs to be scoped to tracked repos only (counting all repos would unfairly penalize miners with legitimate open issues on personal projects).

**PAT constraint:** Timeline API (`timelineItems` GraphQL) works reliably with classic PATs (validators) but has known issues with fine-grained PATs (miners). All timeline-dependent detection must run on the validator PAT. The validator PAT budget is 5,000 requests/hour.

### Strategy: Repo-Centric Closed Scan (Cases 2 & 3)

**Approach:** Scan each tracked repo's closed issues using the validator PAT.

```
GET /repos/{owner/repo}/issues?state=closed&since={lookback_date}&per_page=100
```

The `since` filter scopes to the lookback window (~35 days), keeping page counts manageable. Filter results client-side against known miner GitHub IDs. For each miner-authored closed issue:
- If it matches a merged PR already in memory (from case 1) → skip, already counted
- If not → call `find_solver_from_cross_references()` (timeline API, validator PAT) to check if any PR (miner or not) solved it
  - If solver found → positive credibility (case 2)
  - If no solver → negative credibility (case 3)

**The fallback timeline call is what makes this expensive.** The scan itself is cheap, but distinguishing case 2 from case 3 requires 1 GraphQL call per unmatched issue.

### Budget Stress Test (256 tracked repos, validator PAT)

Based on real closed issue volumes sampled 2026-04-08:

**Sampled repos (closed issues in 35 days):**
- Heavy: openclaw=5978, zed=737, grafana=479, ClickHouse=466, deno=440, llama.cpp=397, pandas=320
- Medium: paperclip=166, astro=160, nanoclaw=146, llama_index=139, beam=139, openlibrary=118
- Light: bitcoin=56, dbeaver=87, desktop=91, ray=94, hoppscotch=18, subtensor=0

**Scan cost (pagination):**

| Component | Calls | Notes |
|---|---|---|
| 32 known repos | ~122 pages | Based on actual volumes |
| ~224 remaining repos | ~224 pages | Assume 1 page each (light) |
| **Total scan** | **~346 calls (7%)** | |

**Total cost (scan + fallback), by miner adoption rate:**

"Miner rate" = what % of closed issues across all repos are authored by registered miners. Currently near 0%, but issue discovery incentivizes miners to file issues — could rise significantly.

| Miner rate | Fallback calls | Total (scan + fallback + existing) | % of 5,000/hr |
|---|---|---|---|
| 0.5% (current) | ~70 | ~516 | 10.3% ✓ |
| 1% | ~140 | ~586 | 11.7% ✓ |
| 2% | ~280 | ~726 | 14.5% ✓ |
| 5% | ~700 | ~1,146 | 22.9% ✓ |
| 10% | ~1,400 | ~1,846 | 36.9% ✓ |
| 20% | ~2,800 | ~3,246 | 64.9% ✓ |

**At 256 repos, budget stays under 75% even at 20% miner adoption.** The previous estimates that showed budget pressure were based on the old 1,375-repo list (revamp-repo-list branch reduces to ~256).

**Assumptions:** These estimates assume the revamp-repo-list branch is merged before issue discovery ships. If the repo list stays at 1,375, the scan alone costs ~2,129 calls (43%) and the fallback pushes past 75% at moderate miner adoption.

### Open Issue Counting (Case 4) — Unsolved

Need: count of a miner's open issues **on tracked repos only**. Global count (all repos) would unfairly penalize miners with legitimate open issues on personal projects.

**Options considered:**

| Option | How | Cost (per miner) | Cost (256 miners) | Drawback |
|---|---|---|---|---|
| **A. Global count on miner query** | Add `issues(states: [OPEN]) { totalCount }` to existing User node GraphQL query (miner PAT) | ~0 extra calls | ~0 | Counts ALL repos, not just tracked. Unfair to miners with personal projects. |
| **B. Batched per-repo GraphQL** | Alias ~20-30 repos per query: `repo1: repository(...) { issues(filterBy: {createdBy: $login}, states: OPEN) { totalCount } }` (miner PAT) | ~9-13 calls | ~2,300-3,300 on miner PATs | Significant per-miner cost. GraphQL complexity limits may cap batch size lower. |
| **C. Search API** | `GET /search/issues?q=author:{login}+is:issue+is:open+repo:{repo1}+repo:{repo2}` (miner PAT) | Multiple calls (max ~5 `repo:` qualifiers per query) | High, 30 req/min search limit | Very slow, separate rate limit (30/min shared). |
| **D. Defer for v1** | Don't implement open issue spam threshold at launch. Rely on credibility gate (80%) + qualification runway (7 solved issues) to catch spammers. Add threshold when mirror ships. | 0 | 0 | No spam threshold — but credibility + qualification gates still filter aggressively. |

**No decision yet.** Option A is near-free but unfairly scoped. Option B works but is expensive. Option D is pragmatic if the credibility gates are sufficient. Needs decision.

### Long-Term: GitHub Mirror (Non-Issue)

The `github-mirror-spec.md` describes a webhook-based mirror service that captures all issue events in real-time via a GitHub App. When the mirror ships, ALL four cases become simple database queries — zero GitHub API calls, zero rate limit concerns. The mirror's `issues` table captures state, author, transfers, and timestamps via webhooks. Validators query the mirror's REST API instead of GitHub.

The current API strategy is a bridge until the mirror is ready (estimated months away).

---

## Data Model

> **Status: IMPLEMENTED** (2026-04-09) — All data model changes below have been implemented across all 4 repos on the `issue-discovery` branch: `gittensor-db` (schema), `gittensor` (classes.py, queries.py, repository.py), `das-gittensor` (TypeORM entities + miners query), `gittensor-ui` (MinerEvaluation type).

### MinerEvaluation — New Fields (`classes.py` + `miner_evaluations` table)

The existing `MinerEvaluation` tracks PR-based scoring. Issue discovery adds a parallel set of fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `issue_discovery_score` | `float` / `DECIMAL(15,6)` | 0.0 | Final aggregated issue discovery score (sum of all scored issues) |
| `issue_credibility` | `float` / `DECIMAL(15,6)` | 0.0 | `solved_issues / (solved_issues + closed_issues - mulligan)` |
| `is_issue_eligible` | `bool` / `BOOLEAN` | False | Meets issue discovery gates (≥7 valid solved issues AND ≥80% issue_credibility) |
| `total_solved_issues` | `int` / `INTEGER` | 0 | Issues resolved via merged PR (positive credibility) |
| `total_closed_issues` | `int` / `INTEGER` | 0 | Issues closed without merged PR or transferred (negative credibility) |
| `total_open_issues` | `int` / `INTEGER` | 0 | Currently open issues by this miner (for spam threshold) |

These are independent from the existing PR fields — a miner has both `credibility` (PR-based, 90% threshold) and `issue_credibility` (issue-based, 80% threshold).

### Issues Table — New Fields (`classes.py` Issue class + `issues` table)

The existing `issues` table stores issue-to-PR relationships. Issue discovery needs additional fields for scoring:

| Field | Type | Default | Description |
|---|---|---|---|
| `author_github_id` | `VARCHAR(255)` | NULL | Issue author's GitHub user ID (for miner matching) |
| `is_transferred` | `BOOLEAN` | FALSE | Whether issue was transferred (timeline API `TransferredEvent`) |
| `updated_at` | `TIMESTAMP` | NULL | GitHub's `updated_at` — rough proxy for edit detection |
| `discovery_base_score` | `DECIMAL(15,6)` | 0.0 | Base score inherited from solving PR |
| `discovery_earned_score` | `DECIMAL(15,6)` | 0.0 | Final score after all multipliers |
| `discovery_review_quality_multiplier` | `DECIMAL(15,6)` | 1.0 | Cliff model: `1.1` clean, then `1.0 - 0.15n` |
| `discovery_repo_weight_multiplier` | `DECIMAL(15,6)` | 1.0 | Inherited from solving PR's repo weight |
| `discovery_time_decay_multiplier` | `DECIMAL(15,6)` | 1.0 | Anchored to solving PR's merge date |
| `discovery_credibility_multiplier` | `DECIMAL(15,6)` | 1.0 | Based on `issue_credibility` |
| `discovery_open_issue_spam_multiplier` | `DECIMAL(15,6)` | 1.0 | 0.0 if over open issue threshold |

### Issue Class — New Fields (`classes.py`)

Implemented on the existing `Issue` dataclass:

```python
# Miner matching
author_github_id: Optional[str] = None

# Edit/transfer detection
is_transferred: bool = False
updated_at: Optional[datetime] = None

# Discovery scoring (populated during issue scoring pipeline)
discovery_base_score: float = 0.0
discovery_earned_score: float = 0.0
discovery_review_quality_multiplier: float = 1.0
discovery_repo_weight_multiplier: float = 1.0
discovery_time_decay_multiplier: float = 1.0
discovery_credibility_multiplier: float = 1.0
discovery_open_issue_spam_multiplier: float = 1.0
```

### Key Design Notes

1. **Issues table PK stays `(number, pr_number, repository_full_name)`** — one issue can be linked from multiple PRs, but only the most recent merged PR's score flows into discovery scoring.
2. **`author_github_id`** (not `author_login`) is used for miner matching because GitHub IDs are immutable while logins can change. The existing `author_login` field is kept for display.
3. **Discovery multipliers are stored per-issue** (not just per-miner) for auditability — the dashboard can show exactly why each issue got its score.
4. **No new tables needed** — issue discovery piggybacks on the existing `issues` and `miner_evaluations` tables with additional columns.

---

## Open / Needs Decision

### Open Issue Counting (Case 4)
How to count a miner's open issues scoped to tracked repos only. See options table in API strategy section above. Needs decision before implementation — option D (defer) is pragmatic if credibility gates are sufficient for v1.

### One-Issue-Per-PR Selection Heuristic
When a PR solves multiple issues, which issue gets the discovery score? Options: earliest-created, most engagement, or split score. Needs decision before implementation.

### Repo-Centric Scan Cadence
Should the closed issue scan run every scoring cycle or less frequently (e.g., every 3rd cycle)? Running every cycle is simpler but uses more budget. Less frequent delays negative credibility signals but saves API calls.

---

## Blocked On / Prerequisites

### Revamp Repo List (revamp-repo-list branch)
The API budget estimates assume ~256 tracked repos. If the repo list stays at 1,375, the closed scan alone costs ~2,129 calls (43%) and the fallback pushes past 75% at moderate miner adoption. **Issue discovery should ship after the repo list revamp.**

### Dynamic Emissions Removal
`dynamic_emissions.py` still needs to be removed and replaced with the hardcoded 30/30/15/25 split in code.

---

## Deferred Post-Launch

- **Edit detection upgrade** — current `updated_at` proxy false-positives on bot activity, comments, labels. Future: timeline/events API for body-only edits.
- **Retroactive linking timing** — if a PR merges in cycle N and issue link appears in cycle N+3, what base score is used?
- **Open issue spam threshold** — if deferred from v1 (option D), add once mirror ships and scoped counting is free.
