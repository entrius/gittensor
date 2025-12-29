# Testing

## Running Tests

Run all tests:

```bash
pytest tests/
```

Run with verbose output:

```bash
pytest tests/ -v
```

Run specific test file:

```bash
pytest tests/utils/test_github_api_tools.py
```

Run specific test class:

```bash
pytest tests/utils/test_github_api_tools.py::TestGraphQLRetryLogic
```

Run tests matching a pattern:

```bash
pytest tests/ -k "demotion"
```

## Adding New Tests

Create test files following the pattern `test_<module_name>.py` in the appropriate directory. Tests are automatically discovered by the test runner.

## Using score calculator (BETA)

- set pat as env variable

```
GITHUB_PAT=<your_pat>
```

- run the script

```
python -m tests.score_calculator <owner/name> <merged_pr_number>
```
