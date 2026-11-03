"""Plain-text invoice line parsing."""


def parse_amount(text):
    """Parse a single amount from ``text``; return None when it is not a number."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_line(text):
    """Parse a ``name: amount`` line into a ``(name, amount)`` pair."""
    name, _, rest = text.partition(":")
    return (name.strip(), parse_amount(rest))


def parse_range(text):
    """Parse a range like ``10-20`` into ``[10.0, 20.0]``; a plain number into ``[value]``.

    Returns None when the text does not parse.
    """
    text = text.strip()
    if "-" in text:
        low, _, high = text.partition("-")
        low_value = parse_amount(low)
        high_value = parse_amount(high)
        if low_value is None or high_value is None:
            return None
        return [low_value, high_value]
    value = parse_amount(text)
    return [value] if value is not None else None
