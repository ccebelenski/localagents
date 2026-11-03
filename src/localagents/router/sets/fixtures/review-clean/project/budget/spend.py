"""Record and hold spend entries."""


class Ledger:
    def __init__(self):
        self._entries = []

    def record(self, month, amount):
        """Record ``amount`` spent in ``month``; return the entry pair."""
        entry = (month, amount)
        self._entries.append(entry)
        return entry

    def entries(self):
        """Return a copy of the recorded ``(month, amount)`` pairs."""
        return list(self._entries)
