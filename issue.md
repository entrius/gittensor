---
name: Refactoring / technical debt
about: Track structural improvements across the Python validator, CLI, and neuron boundary
labels: refactor
title: '[Refactor] Technical debt — validator, CLI, and neuron boundaries'
---

## Summary

Gittensor combines a publishable Python package (`gittensor`), Bittensor neuron entrypoints (`neurons/`), optional PostgreSQL storage, GitHub-backed scoring (OSS PRs, issue discovery, on-chain issue competitions), and a Rust contract crate (`smart-contracts/issues-v0`). This issue captures refactoring themes so work can be split into small PRs without losing the big picture.

## Motivation

- **Clarity at boundaries**: Validator orchestration (`gittensor/validator/forward.py`), domain scoring (`oss_contributions/`, `issue_discovery/`, `issue_competitions/`), and the `neurons/` runtime are related but serve different roles; tightening imports and naming reduces onboarding cost.
- **Configuration**: Neuron argparse wiring lives in `gittensor/utils/config.py`, while validator runtime env reads and side effects (logging at import time) live in `gittensor/validator/utils/config.py`. Unifying or clearly documenting the split avoids “which config is this?” confusion.
- **Maintainability**: Large modules (for example `gittensor/classes.py`) mix domain models, GitHub parsing, and scoring-adjacent logic; splitting by subdomain improves test focus and reviewability.
- **Testability**: Moving emission-blending constants and hardcoded pool splits toward named configuration or `constants.py` makes behavior changes safer to regression-test.

## Current architecture (high level)

| Area | Role |
|------|------|
| `gittensor/cli/` | Click CLI (`gitt` entrypoint): miner PAT flow, issue bounty commands, admin |
| `gittensor/validator/` | Scoring rounds, storage, PAT handling, weights JSON, tree-sitter helpers |
| `neurons/` | Bittensor validator/miner base classes; `neurons/validator.py` wires `forward` and axon handlers |
| `smart-contracts/issues-v0` | Substrate/Rust types and contract surface for issue competitions |
| `tests/` | Pytest: validator units, CLI, GitHub tooling mocks |

## Proposed work (split into separate PRs)

Use this as a checklist; each item should ideally be one focused PR.

- [ ] **Document the two config systems** in code comments or a short contributor note: when to use `gittensor.utils.config` vs `gittensor.validator.utils.config`, and which env vars affect validators only.
- [ ] **Defer validator config side effects**: avoid logging every constant at module import in `gittensor/validator/utils/config.py`; initialize from a small `load_validator_settings()` (or similar) called from `Validator.__init__` so imports stay side-effect free and tests stay quieter.
- [ ] **Reduce neuron ↔ package coupling**: `gittensor/validator/forward.py` uses `TYPE_CHECKING` against `neurons.validator.Validator`; evaluate a narrow protocol or callback interface so core logic does not depend on the concrete neuron class.
- [ ] **Extract emission / pool blending** from `forward()` into a dedicated module or pure functions with explicit inputs/outputs, backed by constants in `gittensor/constants.py` (or a single `emissions.py`) and tests that lock percentages and UID targets.
- [ ] **Split `gittensor/classes.py`** into a `gittensor/models/` (or similar) package: PR/issue dataclasses, enums, cache types, and helpers; keep public re-exports in `classes.py` temporarily if needed to avoid a breaking change for external imports.
- [ ] **Storage layer**: consolidate `gittensor/validator/storage/` and `gittensor/validator/utils/storage.py` responsibilities behind one small facade (connection lifecycle, write paths, feature flags like `STORE_DB_RESULTS`).
- [ ] **GitHub client surface**: `gittensor/utils/github_api_tools.py` is a natural boundary; ensure scoring modules depend on a thin interface where mocks in `tests/utils/` already apply, and trim duplicate URL/string building.
- [ ] **Bittensor weight utilities**: address the `TODO` in `neurons/base/validator.py` regarding numpy migration when upstream `bittensor` APIs stabilize.
- [ ] **Smart contracts**: keep Rust crate versioning and Python `contract_client` in sync; document the handoff (types, events, runtime calls) in one place for refactors that touch both sides.

## Acceptance criteria

- No change to documented scoring semantics or on-chain behavior unless explicitly scoped in a follow-up issue.
- Existing pytest suite and CI workflows (lint, tests, Docker) remain green.
- Any new modules follow existing style: Ruff (`pyproject.toml`), type hints where the file already uses them, single quotes per project convention.

## Out of scope (unless agreed separately)

- Rewriting the reward algorithm or emission percentages.
- Replacing PostgreSQL or GitHub with other backends.
- Large dependency version bumps beyond what CI already pins.

## Useful entrypoints for reviewers

- Validator loop: `gittensor/validator/forward.py`, `neurons/validator.py`
- OSS scoring: `gittensor/validator/oss_contributions/`
- Issue discovery: `gittensor/validator/issue_discovery/`
- Issue competitions + chain: `gittensor/validator/issue_competitions/`, `smart-contracts/issues-v0/`
- CLI: `gittensor/cli/main.py`

## Additional context

- Python 3.12+, packaged with `uv` / `hatchling` (`pyproject.toml`).
- Optional DB: `gittensor/validator/storage/database.py` + env vars `DB_*`.
- Weight inputs: JSON under `gittensor/validator/weights/` (master repos, language weights, token weights).
