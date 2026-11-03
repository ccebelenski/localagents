"""The budget suite."""

import unittest

import budget


class SummaryTests(unittest.TestCase):
    def test_total_by_month(self):
        entries = [("jan", 10.0), ("jan", 5.0), ("feb", 7.0)]
        self.assertEqual(budget.total_by_month(entries),
                         {"jan": 15.0, "feb": 7.0})

    def test_average(self):
        self.assertAlmostEqual(budget.average([("jan", 4.0), ("feb", 8.0)]), 6.0)

    def test_average_empty(self):
        self.assertEqual(budget.average([]), 0.0)


class LedgerTests(unittest.TestCase):
    def test_record_and_entries(self):
        ledger = budget.Ledger()
        ledger.record("jan", 3.0)
        self.assertEqual(ledger.entries(), [("jan", 3.0)])


if __name__ == "__main__":
    unittest.main()
