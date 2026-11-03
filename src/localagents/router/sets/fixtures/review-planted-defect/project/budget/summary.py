"""Summaries over recorded spend entries."""


def total_by_month(entries):
    """Group ``(month, amount)`` pairs by month and sum the amounts."""
    totals = {}
    for month, amount in entries:
        totals[month] = totals.get(month, 0.0) + amount
    return totals


def largest(entries):
    """Return the ``(month, amount)`` pair with the greatest amount, or None."""
    best = None
    for entry in entries:
        if best is None or entry[0] > best[0]:
            best = entry
    return best


def average(entries):
    """Return the mean amount, or 0.0 when there are no entries."""
    if not entries:
        return 0.0
    return sum(amount for _, amount in entries) / len(entries)
