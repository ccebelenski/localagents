"""The unitconv suite."""

import unittest

import unitconv


class TempTests(unittest.TestCase):
    def test_celsius_to_fahrenheit(self):
        self.assertEqual(unitconv.celsius_to_fahrenheit(100), 212.0)

    def test_fahrenheit_to_celsius(self):
        self.assertAlmostEqual(unitconv.fahrenheit_to_celsius(32), 0.0)

    def test_celsius_to_kelvin(self):
        self.assertAlmostEqual(unitconv.celsius_to_kelvin(0), 273.15)

    def test_fahrenheit_to_kelvin(self):
        self.assertAlmostEqual(unitconv.fahrenheit_to_kelvin(32), 273.15)
        self.assertAlmostEqual(unitconv.fahrenheit_to_kelvin(212), 373.15)


class LengthTests(unittest.TestCase):
    def test_meters_to_feet(self):
        self.assertAlmostEqual(unitconv.meters_to_feet(1), 3.280839895)

    def test_round_trip(self):
        self.assertAlmostEqual(unitconv.feet_to_meters(unitconv.meters_to_feet(3.5)), 3.5)


class MoneyTests(unittest.TestCase):
    def test_convert(self):
        self.assertAlmostEqual(unitconv.convert(100, "USD", "EUR"), 92.0)

    def test_same_currency(self):
        self.assertEqual(unitconv.convert(42.5, "GBP", "GBP"), 42.5)


if __name__ == "__main__":
    unittest.main()
