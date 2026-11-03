import unittest

from intervals import merge_intervals


class TestSuite(unittest.TestCase):
    def test_merge_overlapping(self):
        self.assertEqual(merge_intervals([(1, 3), (2, 6), (8, 10)]), [(1, 6), (8, 10)])

    def test_merge_touching(self):
        self.assertEqual(merge_intervals([(1, 2), (2, 5)]), [(1, 5)])

    def test_merge_unsorted(self):
        self.assertEqual(merge_intervals([(8, 10), (1, 3), (2, 6)]), [(1, 6), (8, 10)])

    def test_empty(self):
        self.assertEqual(merge_intervals([]), [])


if __name__ == "__main__":
    unittest.main()
