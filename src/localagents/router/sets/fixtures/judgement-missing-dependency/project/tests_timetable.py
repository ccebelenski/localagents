"""Tests for the timetable package (ScheduleTests)."""

import unittest

import timetable


class ScheduleTests(unittest.TestCase):
    def test_build_schedule(self):
        self.assertEqual(timetable.build_schedule(["a", "b"], days=2), [["a"], ["b"]])

    def test_build_schedule_wraps(self):
        self.assertEqual(timetable.build_schedule(["a", "b", "c"], days=2),
                         [["a", "c"], ["b"]])

    def test_format_schedule(self):
        self.assertEqual(timetable.format_schedule(["a", "b"], days=2),
                         ["day 1: a", "day 2: b"])


if __name__ == "__main__":
    unittest.main()
