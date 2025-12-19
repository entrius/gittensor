import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from gittensor.validator.evaluation.scoring import calculate_time_decay_multiplier
from gittensor.classes import PullRequest
from gittensor.validator.utils.datetime_utils import CHICAGO_TZ, parse_github_timestamp


class TestTimezoneConsistency(unittest.TestCase):
    def test_calculate_time_decay_multiplier_chicago(self):
        # Create a mock PR with merged_at in Chicago timezone
        merged_at = datetime.now(CHICAGO_TZ) - timedelta(hours=10)
        pr = MagicMock(spec=PullRequest)
        pr.merged_at = merged_at

        # Calculate multiplier
        multiplier = calculate_time_decay_multiplier(pr)

        # We expect some decay after 10 hours (grace period is 4 hours)
        self.assertLess(multiplier, 1.0)
        self.assertGreater(multiplier, 0.0)

    def test_parse_github_timestamp_returns_chicago(self):
        timestamp_str = "2024-01-15T10:30:00Z"
        dt = parse_github_timestamp(timestamp_str)

        # Check if it's Chicago
        # pytz timezones can be tricky to compare directly with == due to dst,
        # but checking the zone name or using .zone attribute works.
        self.assertEqual(dt.tzinfo.zone, CHICAGO_TZ.zone)

        # 10:30 UTC is 04:30 Chicago (CST, UTC-6) in January
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.hour, 4)
        self.assertEqual(dt.minute, 30)


if __name__ == '__main__':
    unittest.main()
