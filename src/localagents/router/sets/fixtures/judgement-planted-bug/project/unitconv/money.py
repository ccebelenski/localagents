"""Currency helpers."""

RATES = {
    ("USD", "EUR"): 0.92,
    ("EUR", "USD"): 1.087,
    ("USD", "GBP"): 0.79,
    ("GBP", "USD"): 1.266,
}


def convert(amount, from_ccy, to_ccy):
    """Convert ``amount`` between two currencies at the table rate."""
    if from_ccy == to_ccy:
        return amount
    return amount * RATES[(from_ccy, to_ccy)]


def percent_of(part, whole):
    """Return ``part`` as a percentage of ``whole`` (percent_of(1, 4) is 25.0)."""
    return part / whole
