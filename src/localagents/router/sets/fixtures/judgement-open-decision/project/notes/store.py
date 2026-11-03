"""An in-memory note store."""


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
