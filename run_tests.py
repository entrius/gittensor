#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Test runner for Gittensor unit tests

Usage:
    python run_tests.py                    # Run all tests
    python run_tests.py -v                 # Run with verbose output
    python run_tests.py tests.utils        # Run specific test module
    python run_tests.py tests.utils.test_github_api_tools.TestGraphQLRetryLogic  # Run specific test class
"""

import sys
import unittest

if __name__ == '__main__':
    # Discover and run tests
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        # Run specific test module or class
        suite = unittest.TestLoader().loadTestsFromName(sys.argv[1])
    else:
        # Discover all tests in the tests directory
        loader = unittest.TestLoader()
        suite = loader.discover('tests', pattern='test_*.py')

    # Run tests with appropriate verbosity
    verbosity = 2 if '-v' in sys.argv or '--verbose' in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # Exit with appropriate code
    sys.exit(0 if result.wasSuccessful() else 1)
