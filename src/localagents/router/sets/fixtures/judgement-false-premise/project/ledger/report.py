"""Build a simple total from parsed invoice lines."""

from .parsing import parse_line


def total(lines):
    """Sum the amounts of a list of ``name: amount`` lines, skipping unparseable ones."""
    grand = 0.0
    for line in lines:
        _, amount = parse_line(line)
        if amount is not None:
            grand += amount
    return grand
