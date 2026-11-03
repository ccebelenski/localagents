"""The report library's checks: run with python3 -m unittest -q build_check."""

import unittest

import fmtutil
import report


class ReportChecks(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(report.subtotal([1.1, 2.2, 3.3]), 6.6)

    def test_render(self):
        self.assertEqual(report.render(42.0), "Total: USD 42.00")

    def test_money_defaults(self):
        self.assertEqual(fmtutil.money(7.5), "USD 7.50")


if __name__ == "__main__":
    unittest.main()
