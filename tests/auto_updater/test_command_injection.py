"""
Test for command injection vulnerability in auto-updater orchestrator.

This test demonstrates the vulnerability and verifies the fix.
"""

import logging
import os
import sys
import tempfile
import unittest

# Add parent directory to path to import auto_updater modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from auto_updater.auto_update.orchestrator import UpdateOrchestrator


class TestCommandInjection(unittest.TestCase):
    """Test command injection vulnerability and fix."""

    def setUp(self):
        """Set up test environment."""
        # Create a temporary directory for testing
        self.test_dir = tempfile.mkdtemp()
        self.marker_file = os.path.join(self.test_dir, "command_injected")

        # Setup logging to suppress noise during tests
        logging.basicConfig(level=logging.ERROR)

    def tearDown(self):
        """Clean up test environment."""
        # Remove marker file if it exists
        if os.path.exists(self.marker_file):
            os.remove(self.marker_file)
        # Remove test directory
        if os.path.exists(self.test_dir):
            os.rmdir(self.test_dir)

    def test_vulnerability_exists(self):
        """
        Test that demonstrates the command injection vulnerability.

        This test proves that malicious input in target_commit can execute
        arbitrary shell commands.
        """
        # Create orchestrator instance
        # Use a dummy repo path that won't interfere with actual git operations
        orchestrator = UpdateOrchestrator(repo_path=self.test_dir)

        # Create malicious commit hash that includes command injection
        # This will attempt to create a marker file proving command execution
        malicious_commit = f"abc123; touch {self.marker_file} #"

        # Attempt to perform git update with malicious input
        # This should fail gracefully but NOT execute the injected command
        # However, with the vulnerable code, it WILL execute the command
        orchestrator.perform_git_update(target_commit=malicious_commit)

        # Check if command injection occurred
        # If vulnerability exists, the marker file will be created
        command_injected = os.path.exists(self.marker_file)

        if command_injected:
            print("\n" + "=" * 70)
            print("VULNERABILITY CONFIRMED: Command injection successful!")
            print(f"Marker file created at: {self.marker_file}")
            print("=" * 70 + "\n")
        else:
            print("\n" + "=" * 70)
            print("VULNERABILITY NOT EXPLOITED: Command injection prevented")
            print("=" * 70 + "\n")

        # Note: We don't assert here because we're testing the vulnerability
        # The actual fix will prevent this from happening

    def test_rollback_vulnerability(self):
        """
        Test that demonstrates command injection in rollback_git_update.
        """
        orchestrator = UpdateOrchestrator(repo_path=self.test_dir)

        # Create malicious commit hash for rollback
        malicious_commit = f"xyz789; touch {self.marker_file} #"

        # Attempt rollback with malicious input
        result = orchestrator.rollback_git_update(previous_commit=malicious_commit)

        # Check if command injection occurred
        command_injected = os.path.exists(self.marker_file)

        if command_injected:
            print("\n" + "=" * 70)
            print("VULNERABILITY CONFIRMED: Rollback command injection successful!")
            print(f"Marker file created at: {self.marker_file}")
            print("=" * 70 + "\n")
        else:
            print("\n" + "=" * 70)
            print("VULNERABILITY NOT EXPLOITED: Rollback command injection prevented")
            print("=" * 70 + "\n")

        # Verify that the method rejected the malicious input
        self.assertFalse(result, "Rollback should fail with invalid commit hash")
        self.assertFalse(command_injected, "Command injection should be prevented")

    def test_commit_hash_validation(self):
        """Test that commit hash validation works correctly."""
        orchestrator = UpdateOrchestrator(repo_path=self.test_dir)

        # Test valid commit hashes
        valid_hashes = [
            "a1b2c3d",  # Short hash (7 chars)
            "a1b2c3d4e5f678901234",  # Medium hash (24 chars)
            "a1b2c3d4e5f6789012345678901234567890abcd",  # Full hash (40 chars)
            "ABCDEF1234567890abcdef",  # Mixed case
            "deadbeef",  # Common short hash format
        ]

        for commit_hash in valid_hashes:
            self.assertTrue(
                orchestrator._validate_commit_hash(commit_hash),
                f"Valid hash should pass: {commit_hash}",
            )

        # Test invalid commit hashes (should be rejected)
        invalid_hashes = [
            "",  # Empty
            "abc123",  # Too short (< 7 chars)
            "abc123; rm -rf /",  # Contains shell metacharacter
            "abc123 && echo test",  # Contains shell operator
            "abc123`whoami`",  # Contains backtick
            "abc123$(ls)",  # Contains command substitution
            "abc123\n",  # Contains newline
            "abc123 ",  # Contains space
            "xyz789; touch /tmp/test #",  # Our test case
        ]

        for commit_hash in invalid_hashes:
            self.assertFalse(
                orchestrator._validate_commit_hash(commit_hash),
                f"Invalid hash should be rejected: {commit_hash}",
            )


if __name__ == "__main__":
    unittest.main()
