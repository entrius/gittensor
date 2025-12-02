# Gittensor Contributor Guide

## Getting Started

Before contributing, please:

1. Read the [README](README.md) to understand the project goals
2. Review the [miner guide](gittensor/miner/README.md) and [validator guide](gittensor/validator/README.md)
3. Check existing issues and discussions to avoid duplicate work

## Lifecycle of a Pull Request

### 1. Create Your Branch

- Branch off of `test` and target `test` with your PR
- Ensure there are no conflicts before submitting

### 2. Make Your Changes

- Write clean, well-documented code
- Follow existing code patterns and architecture
- Update documentation if applicable
- Do NOT add comments that are over-explanatory, redundant, or extraneous to inflate line count.
- We will NOT accept PRs that are purely testing changes, documentation changes, or typo fixes
- When making your changes, ask yourself: will this raise the value of the repository?

### 3. Submit Pull Request

1. Push your branch to the repository
2. Open a PR targeting the `test` branch
3. Use draft PRs for work-in-progress changes
4. Fill out the PR template with:
   - Clear description of changes
   - Motivation and context
   - Related issues (if any)
   - Testing performed

### 4. Code Review

- Assign `anderdc` and `landyndev` to your PR for review

## PR Labels

Apply appropriate labels to help categorize and track your contribution:

- `feature` - New feature additions or enhancements
- `bug` - Bug fixes
- `refactor` - Code refactoring without functionality changes
- `documentation` - Documentation improvements

## Code Standards

### Quality Expectations

- Follow repository conventions (commenting style, variable naming, etc.)
- Use sensible helper functions to modularize code
- Code that is drawn out or obviously unoptimized to artificially inflate line count will be rejected
- Write clean, readable, maintainable code
- Avoid modifying unrelated files
- Avoid adding unnecessary dependencies

## Branches

### `test`

**Purpose**: Testing and staging for main

**Restrictions**:

- Requires pull request

### `main`

**Purpose**: Production-ready code

**Restrictions**:

- Requires pull request
- Only maintainers can update

## License

By contributing to Gittensor, you agree that your contributions will be licensed under the project's license.

---

Thank you for contributing to Gittensor and helping advance open source software development!
