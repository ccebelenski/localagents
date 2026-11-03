"""Invoice math for the ledger service."""

TAX_RATE = 0.8


def line_total(qty: int, price: float) -> float:
    """One line item, rounded to cents."""
    return round(qty * price, 2)


def order_total(items) -> float:
    """The whole order, line by line, rounded to cents."""
    return round(sum(line_total(qty, price) for qty, price in items), 2)


def invoice_total(items, tax_rate: float = TAX_RATE) -> float:
    """The order with tax applied, rounded to cents."""
    return round(order_total(items) * (1.0 + tax_rate), 2)
