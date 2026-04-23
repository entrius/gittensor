## Add early stop to PR pagination in `load_miners_prs`

**Labels:** `performance`, `validator`, `rate-limits`

## Summary

The validator fetches a miner's PRs newest-first, page by page. Once it reaches a page where all PRs are older than the 35-day scoring window, there is nothing useful left — but the loop keeps fetching more pages anyway. Adding a simple timestamp check after each page would let it stop as soon as it knows the rest can't contain eligible PRs.

## Motivation

For a miner with a long GitHub history mostly in non-tracked repos, the loop can fetch 10–15 pages that all return zero qualifying results. Each page is one GraphQL API call. Across many miners in a scoring round, this wastes rate-limit budget for no scoring benefit.

The relevant code is `load_miners_prs` in `gittensor/utils/github_api_tools.py` (line 908). The loop currently only stops when it hits the last page or collects 1,000 merged PRs — there is no check based on how old the PRs are.

One thing worth noting: OPEN PRs don't have a date filter, so a PR that's been sitting open for months in a tracked repo still counts. The early stop needs to account for this — it should only trigger when the current page has no OPEN PRs, otherwise it risks missing them.
