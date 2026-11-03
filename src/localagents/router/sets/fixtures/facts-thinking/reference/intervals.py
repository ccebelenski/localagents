"""Interval helpers (reference solution)."""


def merge_intervals(intervals):
    """Merge overlapping or touching closed intervals.

    ``intervals`` is a list of ``(start, end)`` pairs of ints with ``start <= end``. Return the
    merged intervals as a list of pairs sorted by start, merging both overlapping intervals
    (``[(1, 3), (2, 6)]`` -> ``[(1, 6)]``) and touching ones (``[(1, 2), (2, 5)]`` ->
    ``[(1, 5)]``). An empty list gives an empty list.
    """
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
