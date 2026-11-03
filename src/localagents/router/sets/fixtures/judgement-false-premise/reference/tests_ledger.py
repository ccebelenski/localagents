"""The ledger suite."""

import unittest

import ledger


class ParsingTests(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(ledger.parse_amount("42.5"), 42.5)

    def test_parse_amount_none(self):
        self.assertIsNone(ledger.parse_amount("nope"))

    def test_parse_line(self):
        self.assertEqual(ledger.parse_line("widget: 3"), ("widget", 3.0))

    def test_parse_range(self):
        self.assertEqual(ledger.parse_range("10-20"), [10.0, 20.0])
        self.assertEqual(ledger.parse_range("7"), [7.0])
        self.assertIsNone(ledger.parse_range("ten-twelve"))


class ReportTests(unittest.TestCase):
    def test_total(self):
        self.assertAlmostEqual(ledger.total(["a: 1", "b: 2", "c: 4.5"]), 7.5)

    def test_total_skips_bad(self):
        self.assertAlmostEqual(ledger.total(["a: 1", "bad"]), 1.0)


if __name__ == "__main__":
    unittest.main()
