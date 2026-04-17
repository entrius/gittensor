# Gittensor Contributor Guide

## Development Setup

1. Install dependencies:
   ```bash
   uv sync --extra dev
   ```

2. Install git hooks:
   ```bash
   uv run pre-commit install --install-hooks
   ```

   This installs pre-commit and pre-push hooks. Ruff lint/format runs on every
   commit; pyright and pytest run before push.

3. Run all checks manually:
   ```bash
   uv run pre-commit run --all-files                        # pre-commit hooks
   uv run pre-commit run --all-files --hook-stage pre-push  # pre-push hooks
   ```

4. To skip hooks for WIP commits or pushes:
   ```bash
   git commit --no-verify -m "WIP: ..."
   git push --no-verify
   ```

## Getting Started

Before contributing, please:

1. Read the [README](./README.md) to understand the project goals
2. Understand Miner and Validator interactions/design
3. Check existing issues, PRs, and discussions to avoid duplicate work

## Creating Issues

When opening an issue, use the appropriate template:

- **[Bug Report](.github/ISSUE_TEMPLATE/bug_report.md)** - Report bugs or unexpected behavior. Include steps to reproduce, expected vs actual behavior, and environment details.
- **[Feature Request](.github/ISSUE_TEMPLATE/feature_request.md)** - Suggest new features or improvements. Explain the motivation and proposed solution.
- **Blank Issue** - For issues that don't fit the above templates.

For security vulnerabilities, **do not create a public issue**. Report them privately via [GitHub Security Advisories](https://github.com/entrius/gittensor/security/advisories/new).

## Lifecycle of a Pull Request

### 1. Create Your Branch

- Branch off of `test` and target `test` with your PR
- Ensure there are no conflicts before submitting

### 2. Make Your Changes

- Write clean, well-documented code
- Follow existing code patterns and architecture
- Update documentation if applicable
- Update tests and create new tests if there are new functions or older testing is outdated

  _NOTE: We do NOT accept PRs that are only testing changes/additions, they will need to be backed up with a good reason_

- Do NOT add comments that are over-explanatory, redundant
- When making your changes, ask yourself: will this raise the value of the repository?
- Ensure ALL tests pass, the PR will not be accepted if there are failing tests

### 3. Submit Pull Request

1. Push your branch to the repository
2. Open a PR targeting the `test` branch
3. Use draft PRs for work-in-progress changes
4. Fill out the [PR template](.github/PULL_REQUEST_TEMPLATE.md):
   - **Summary**: Clear description of changes
   - **Related Issues**: Link issues using `Fixes #123` or `Closes #456`
   - **Type of Change**: Select bug fix, new feature, refactor, documentation, or other
   - **Testing**: Confirm tests added/updated and manual testing performed
   - **Checklist**: Verify code style, self-review, and documentation

### 4. Code Review

- Assign `anderdc` and `landyndev` to your PR for review

## PR Labels

Apply appropriate labels to help categorize and track your contribution:

- `bug` - Bug fixes
- `feature` - New feature additions
- `enhancement` - Improvements to existing features
- `refactor` - Code refactoring without functionality changes
- `documentation` - Documentation updates

## Contribution Scope

### Out of Scope

The following directories are **not accepting external contributions**:

- `smart-contracts/` — All ink!/Rust smart contracts
- Any other Rust crates in this repository

Changes to these directories will be closed without review. If you have questions or suggestions related to the smart contracts, open a discussion or issue instead.

### In Scope

Contributions are welcome for the Python codebase, including the validator, miner, CLI, and associated tests.

## Code Standards

### Quality Expectations

- Follow repository conventions (commenting style, variable naming, etc.)
- Use sensible helper functions to modularize code
- Code that is drawn out or obviously unoptimized to artificially inflate line count will be rejected
- Write clean, readable, maintainable code
- Avoid modifying unrelated files
- Avoid adding unnecessary dependencies
- Ensure all tests pass

## Branches

### `test`

**Purpose**: Testing and staging for main

**Restrictions**:

- Requires pull request
- Requires tests to pass
- Requires one approval from either `@landyndev` or `@anderdc`

### `main`

**Purpose**: Production-ready code

**Restrictions**:

- Only maintainers can update

## License

By contributing to Gittensor, you agree that your contributions will be licensed under the project's license.

---

Thank you for contributing to Gittensor and helping advance open source software development!
