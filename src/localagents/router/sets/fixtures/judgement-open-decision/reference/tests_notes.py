"""The notes suite."""

import os
import tempfile
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

    def test_save_and_load(self):
        store = notes.Store()
        store.add("a")
        store.add("b")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "notes.json")
            notes.save(store, path)
            loaded = notes.load(path)
        self.assertEqual(loaded.all(), ["a", "b"])
        self.assertEqual(loaded.get(1), "a")
        self.assertEqual(loaded.get(2), "b")


if __name__ == "__main__":
    unittest.main()
