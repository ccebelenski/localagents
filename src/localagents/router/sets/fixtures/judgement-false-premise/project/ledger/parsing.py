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
