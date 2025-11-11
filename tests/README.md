# Testing

## Running Tests

Run all tests:
```bash
python run_tests.py
```

Run with verbose output:
```bash
python run_tests.py -v
```

Run specific test module:
```bash
python run_tests.py tests.utils.test_github_api_tools
```

Run specific test class:
```bash
python run_tests.py tests.utils.test_github_api_tools.TestGraphQLRetryLogic
```

## Using pytest

If you prefer pytest:
```bash
pytest tests/ -v
```

## Adding New Tests

Create test files following the pattern `test_<module_name>.py` in the appropriate directory. Tests are automatically discovered by the test runner.
