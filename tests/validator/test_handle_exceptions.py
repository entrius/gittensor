# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for the validator involving handling exceptions during runtime.

Tests to see if the validator exits properly when an exception happens in the background thread.
"""

import sys
import unittest
from unittest.mock import Mock, call, patch, MagicMock

from bittensor import MockSubtensor

# Mock the circular import dependencies before importing the module
# This prevents the circular import error when running tests
sys.modules["gittensor.validator"] = Mock()
sys.modules["gittensor.validator.utils"] = Mock()
sys.modules["gittensor.validator.utils.config"] = Mock()
sys.modules["gittensor.validator.utils.config"].PR_LOOKBACK_DAYS = 30
sys.modules["gittensor.validator.utils.config"].WANDB_PROJECT = "gittensor-validators"
sys.modules["gittensor.validator.utils.config"].__version__ = "2.0.5"
sys.modules["gittensor.validator.utils.storage"] = Mock()

from gittensor.mock import MockMetagraph
from neurons.validator import main


class TestHandleExceptionAndExit(unittest.TestCase):
    def setUp(self):
        pass

    @patch("gittensor.utils.github_api_tools.requests.post")
    @patch("gittensor.utils.github_api_tools.time.sleep")
    def test_handle_exception_and_exit(self, mock_logging, mock_sleep):
        """Test that the validator exits properly when an exception happens in the background thread."""
        mock_metagraph = MagicMock(hotkeys=MagicMock(index=MagicMock(return_value=0)), n=128)
        mock_subtensor = MagicMock(return_value=MagicMock(metagraph=MagicMock(return_value=mock_metagraph)))
        with patch("bittensor.subtensor", side_effect=mock_subtensor):
            # Have to patch so it runs the thread still
            with patch("neurons.base.neuron.BaseNeuron.check_registered"):
                # Setup so we trigger an excpetion immediately when the validator starts
                with patch("neurons.validator.Validator.sync") as mock_sync:
                    # First call is initial sync, which should succeed
                    mock_sync.side_effect = [None, Exception("Test exception")]
                    main()

            # Failed on second sync, which is at the start of `run`
            # importantly we got to here, which means we exited the while loop in main
            self.assertEqual(mock_sync.call_count, 2)
