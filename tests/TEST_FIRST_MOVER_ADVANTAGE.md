# First-Mover Advantage Testing Guide

## Overview

This document describes how to test the first-mover advantage scoring mechanism implemented in gittensor.

## What is First-Mover Advantage?

The first-mover advantage mechanism incentivizes miners to discover and contribute to new repositories:
- **First contributor** to a repository gets full score (1.0x multiplier)
- **All subsequent contributors** to that same repository get reduced score (0.1x multiplier)

## Test Files

### 1. Unit Tests: `tests/test_first_mover_advantage.py`

Comprehensive unit tests that verify the core logic of the first-mover mechanism.

**Running the tests:**
```bash
python -m unittest tests.test_first_mover_advantage -v
```

**Test Coverage:**

- ✅ **test_single_contributor_unchanged** - Verifies single contributor keeps full score
- ✅ **test_first_mover_vs_follower** - Tests basic first vs follower scenario
- ✅ **test_multiple_repos_independent** - Ensures first-mover status is per-repository
- ✅ **test_tiebreaker_by_uid** - Verifies UID tiebreaker when timestamps match
- ✅ **test_multiple_prs_same_miner_same_repo** - Tests that first mover gets 1.0x on all their PRs
- ✅ **test_three_miners_cascade** - Validates that only first gets 1.0x, all others get 0.1x
- ✅ **test_empty_evaluations** - Edge case: empty data handling
- ✅ **test_miner_with_no_prs** - Edge case: miner without PRs

### 2. Simulation Tests: `gittensor/validator/test/simulation/`

Integration tests using mock PR data to test the scoring system end-to-end.

**Test Cases Added to `mock_prs.py`:**

- **Test Case 7**: First-mover to a new repository (UID 1)
- **Test Case 8**: Follower to an existing repository (UID 2)  
- **Test Case 9**: Mixed status - first to one repo, follower to another (UID 3)

**Running simulation tests:**
```bash
cd gittensor/validator/test/simulation
python test_pr_scoring.py
```

## Key Test Scenarios

### Scenario 1: Simple First vs Follower
```
Repository: owner/repo1
- UID 1 merges PR at 2025-01-01 → Gets 1.0x
- UID 2 merges PR at 2025-01-06 → Gets 0.1x
```

### Scenario 2: Per-Repository Independence
```
Repository A:
- UID 1 is first → 1.0x
- UID 2 is follower → 0.1x

Repository B:
- UID 2 is first → 1.0x
- UID 1 is follower → 0.1x
```

### Scenario 3: Timestamp Tiebreaker
```
Repository: owner/repo1
All PRs merged at 2025-01-01 00:00:00
- UID 3 → 1.0x (lowest UID wins)
- UID 5 → 0.1x
- UID 7 → 0.1x
```

### Scenario 4: Multiple PRs by First Mover
```
Repository: owner/repo1
- UID 1, PR #100, merged 2025-01-01 → 1.0x
- UID 1, PR #101, merged 2025-01-05 → 1.0x (still first mover!)
- UID 1, PR #102, merged 2025-01-10 → 1.0x (still first mover!)
- UID 2, PR #103, merged 2025-01-07 → 0.1x (follower)
```

## Expected Behavior

### Correct Behavior ✅

1. First contributor to a repo gets all their PRs scored at 1.0x
2. All other contributors get 0.1x for all their PRs to that repo
3. First-mover status is independent per repository
4. Earliest merge timestamp determines first mover
5. Lower UID breaks ties when timestamps are identical
6. Empty evaluations are handled gracefully

### Incorrect Behavior ❌

1. Followers getting 1.0x multiplier
2. First mover getting 0.1x multiplier
3. First-mover status carrying across different repositories
4. Second PR by first mover getting 0.1x
5. Crashes on empty data

## Debugging Tips

### Enable Debug Logging

The `apply_first_mover_advantage()` function includes comprehensive logging:

```python
bt.logging.info(f"Identified first movers for {len(repo_first_mover)} repositories:")
bt.logging.info(f"UID {uid} is FIRST MOVER to {repo}")
bt.logging.info(f"UID {uid} is FOLLOWER to {repo} (first: UID {first_mover_uid})")
```

### Breakpoint Locations

Set breakpoints at these key locations:

1. **First pass loop** (determining first movers):
   - Line: `for pr in evaluation.pull_requests:`
   
2. **Comparison logic** (checking timestamps):
   - Line: `if pr.merged_at < current_earliest_time:`
   
3. **Second pass loop** (applying multipliers):
   - Line: `if first_mover_uid == uid:`

### Manual Inspection

Check intermediate data structures:

```python
# After first pass
print(repo_first_mover)
# Output: {'owner/repo1': (1, datetime(2025, 1, 1)), ...}

# Check PR scores before and after
for pr in evaluation.pull_requests:
    print(f"PR {pr.number}: {pr.earned_score}")
```

## Integration with Other Scoring Components

The first-mover advantage is applied in the following order (see `reward.py`):

1. ✅ Base score calculation (file changes × language weights)
2. ✅ Issue resolution bonus
3. ✅ Repository weight
4. ✅ Duplicate account detection
5. ⭐ **First-mover advantage** ← Our mechanism
6. ✅ Time decay
7. ✅ Gittensor tag boost
8. ✅ Pareto normalization
9. ✅ Dynamic emissions

## Constants

From `gittensor/constants.py`:

```python
FIRST_MOVER_FOLLOWER_MULTIPLIER = 0.1  # Followers get 10% of their score
```

## Implementation Files

- **Main logic**: `gittensor/validator/evaluation/scoring.py` - `apply_first_mover_advantage()`
- **Integration**: `gittensor/validator/evaluation/reward.py` - Line 193
- **Constants**: `gittensor/constants.py`
- **Unit tests**: `tests/test_first_mover_advantage.py`
- **Simulation tests**: `gittensor/validator/test/simulation/mock_prs.py`

## Success Criteria

Tests pass if:
1. ✅ All unit tests pass without errors
2. ✅ Simulation tests produce expected score multipliers
3. ✅ First movers consistently get 1.0x across all their PRs
4. ✅ Followers consistently get 0.1x
5. ✅ Tiebreaker logic works correctly
6. ✅ Per-repository independence is maintained

## Maintenance Notes

When updating the first-mover mechanism:

1. Update the constant in `constants.py` if changing the follower multiplier
2. Add test cases to `test_first_mover_advantage.py` for new edge cases
3. Update this documentation
4. Update README.md if behavior changes
5. Consider backward compatibility if changing logic significantly

---

**Last Updated**: 2025-11-17  
**Version**: 1.0  
**Author**: Gittensor Team
