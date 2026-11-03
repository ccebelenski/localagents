"""The build check for ledger-calc: python3 -m unittest -q build_check."""

import unittest

import calc


class OrderTest(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(calc.line_total(3, 2.5), 7.5)

    def test_order_total(self):
        self.assertEqual(calc.order_total([(1, 10.0), (2, 5.0)]), 20.0)


class InvoiceTest(unittest.TestCase):
    def test_invoice_total(self):
        self.assertEqual(calc.invoice_total([(1, 10.0), (2, 5.0)]), 21.6)

    def test_invoice_total_without_tax(self):
        self.assertEqual(calc.invoice_total([(2, 5.0)], tax_rate=0.0), 10.0)


if __name__ == "__main__":
    unittest.main()
