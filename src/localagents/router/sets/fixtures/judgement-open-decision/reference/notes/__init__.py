"""notes: a small in-memory note store, with JSON-file persistence."""

from .store import Store, load, save

__all__ = ["Store", "load", "save"]
