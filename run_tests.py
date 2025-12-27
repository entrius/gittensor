#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Test runner for Gittensor unit tests.

Usage:
    python run_tests.py                     # Run all tests
    python run_tests.py -v                  # Run with verbose output
    python run_tests.py -vv                 # Run with extra verbose output
    python run_tests.py tests/validator     # Run tests in a directory
    python run_tests.py tests/validator/test_tier_credibility.py  # Run specific file
    python run_tests.py tests/validator/test_tier_credibility.py::TestTierDemotion  # Run specific class
    python run_tests.py tests/validator/test_tier_credibility.py::TestTierDemotion::test_gold_demoted_when_credibility_drops  # Run specific test
    python run_tests.py -k "demotion"       # Run tests matching pattern
    python run_tests.py --lf                # Run last failed tests
    python run_tests.py --tb=short          # Short tracebacks
    python run_tests.py -x                  # Stop on first failure
"""

import sys
import subprocess


def main():
    # Base pytest command
    cmd = [sys.executable, "-m", "pytest"]

    # Default to tests directory if no path specified
    args = sys.argv[1:]

    # Check if any positional args (test paths) were provided
    has_test_path = any(
        arg.startswith("tests") or arg.endswith(".py")
        for arg in args
        if not arg.startswith("-")
    )

    if not has_test_path:
        # Run all tests in tests/ directory
        cmd.append("tests/")

    # Add all command line arguments
    cmd.extend(args)

    # Run pytest
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
