"""The notes suite."""

import unittest

import notes


class StoreTests(unittest.TestCase):
    def test_add_and_get(self):
        store = notes.Store()
        note_id = store.add("hello")
        self.assertEqual(store.get(note_id), "hello")

    def test_all(self):
        store = notes.Store()
        store.add("a")
        store.add("b")
        self.assertEqual(store.all(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
