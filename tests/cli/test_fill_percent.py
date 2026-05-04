import unittest
from decimal import Decimal
from gittensor.cli.issue_commands.view import _fill_percent

class TestFillPercent(unittest.TestCase):
    def test_basic_calculation(self):
        self.assertEqual(_fill_percent(50, 100), 50.0)
        self.assertEqual(_fill_percent(1, 3), 33.333333)
        self.assertEqual(_fill_percent(0, 100), 0.0)

    def test_zero_target(self):
        self.assertEqual(_fill_percent(100, 0), 0.0)
        self.assertEqual(_fill_percent(0, 0), 0.0)

    def test_negative_values(self):
        # Even if data should be unsigned, the function should handle it gracefully
        self.assertEqual(_fill_percent(-50, 100), -50.0)
        self.assertEqual(_fill_percent(50, -100), 0.0)

    def test_floating_point_artifacts_avoidance(self):
        # Case that might have artifacts in pure float
        # (0.1 + 0.2) != 0.3 but Decimal(0.1) + Decimal(0.2) == Decimal(0.3)
        # 1/10 * 100 should be exactly 10.0
        self.assertEqual(_fill_percent(1, 10), 10.0)
        
        # Testing a more complex division
        # 1/7 = 0.14285714285...
        # * 100 = 14.285714285...
        res = _fill_percent(1, 7)
        self.assertAlmostEqual(res, 14.285714, places=6)

if __name__ == '__main__':
    unittest.main()
