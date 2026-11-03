"""An in-memory note store, with JSON-file persistence."""

import json


class Store:
    def __init__(self):
        self._notes = {}

    def add(self, text):
        """Store ``text``; return the note's id."""
        note_id = len(self._notes) + 1
        self._notes[note_id] = text
        return note_id

    def get(self, note_id):
        """Return the note with ``note_id``, or None."""
        return self._notes.get(note_id)

    def all(self):
        """Return all note texts, in the order they were added."""
        return list(self._notes.values())


def save(store, path):
    """Write ``store``'s notes to ``path`` as a JSON file."""
    with open(path, "w") as fh:
        json.dump(store._notes, fh)


def load(path):
    """Read the notes from ``path`` into a new ``Store`` with the same note ids."""
    store = Store()
    with open(path) as fh:
        store._notes = {int(key): text for key, text in json.load(fh).items()}
    return store
