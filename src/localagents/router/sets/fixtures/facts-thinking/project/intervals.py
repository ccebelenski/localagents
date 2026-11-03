"""Interval helpers."""


def merge_intervals(intervals):
    """Merge overlapping or touching closed intervals.

    ``intervals`` is a list of ``(start, end)`` pairs of ints with ``start <= end``. Return the
    merged intervals as a list of pairs sorted by start, merging both overlapping intervals
    (``[(1, 3), (2, 6)]`` -> ``[(1, 6)]``) and touching ones (``[(1, 2), (2, 5)]`` ->
    ``[(1, 5)]``). An empty list gives an empty list.
    """
    raise NotImplementedError("merge_intervals")
