"""Monthly report rendering."""

from fmtutil import money


def subtotal(rows: list[float]) -> float:
    return round(sum(rows), 2)


def render(total: float, currency: str = "USD") -> str:
    return f"Total: {money(total, currency)}"
