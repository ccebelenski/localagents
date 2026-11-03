#!/usr/bin/env bash
# tool-bisect's history builder (calibration set spec §3): rebuilds an eight-commit history in
# the materialized repo with the regression planted at commit five. Identity and dates are
# pinned so the planted hash is the same on every machine; the answer sits at refs/probe/answer,
# which log and bisect never show, and the brief never names it. The shipped project/ files are
# the final state: red, the last commit's tree.
set -eu

rm -rf .git
git init -q .
git config user.name "Ana Novak"
git config user.email "ana@example.invalid"

commit() {
    GIT_AUTHOR_DATE="$1 00:00:00 +0000" GIT_COMMITTER_DATE="$1 00:00:00 +0000" \
        git commit -q -m "$2"
}

# the working tree holds the final (red) files; C1 starts from the helpers only
rm -f README.md notes.md

# C1 — the first helpers, no rounding, no tax
cat > calc.py <<'EOF'
"""Invoice math for the ledger service."""


def line_total(qty: int, price: float) -> float:
    return qty * price


def order_total(items) -> float:
    return sum(line_total(qty, price) for qty, price in items)
EOF

cat > build_check.py <<'EOF'
"""The build check for ledger-calc: python3 -m unittest -q build_check."""

import unittest

import calc


class OrderTest(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(calc.line_total(3, 2.5), 7.5)

    def test_order_total(self):
        self.assertEqual(calc.order_total([(1, 10.0), (2, 5.0)]), 20.0)


if __name__ == "__main__":
    unittest.main()
EOF

git add -A
commit 2026-03-02 "ledger: line and order totals"

# C2 — rounding lands, with the README
cat > calc.py <<'EOF'
"""Invoice math for the ledger service."""


def line_total(qty: int, price: float) -> float:
    """One line item, rounded to cents."""
    return round(qty * price, 2)


def order_total(items) -> float:
    """The whole order, line by line, rounded to cents."""
    return round(sum(line_total(qty, price) for qty, price in items), 2)
EOF

cat > README.md <<'EOF'
# ledger-calc

Invoice math for the ledger service.

Check: `python3 -m unittest -q build_check`
EOF

git add -A
commit 2026-03-03 "ledger: round line totals to cents"

# C3 — invoice tax, green
cat > calc.py <<'EOF'
"""Invoice math for the ledger service."""

TAX_RATE = 0.08


def line_total(qty: int, price: float) -> float:
    """One line item, rounded to cents."""
    return round(qty * price, 2)


def order_total(items) -> float:
    """The whole order, line by line, rounded to cents."""
    return round(sum(line_total(qty, price) for qty, price in items), 2)


def invoice_total(items, tax_rate: float = TAX_RATE) -> float:
    """The order with tax applied, rounded to cents."""
    return round(order_total(items) * (1.0 + tax_rate), 2)
EOF

cat > build_check.py <<'EOF'
"""The build check for ledger-calc: python3 -m unittest -q build_check."""

import unittest

import calc


class OrderTest(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(calc.line_total(3, 2.5), 7.5)

    def test_order_total(self):
        self.assertEqual(calc.order_total([(1, 10.0), (2, 5.0)]), 20.0)


class InvoiceTest(unittest.TestCase):
    def test_invoice_total(self):
        self.assertEqual(calc.invoice_total([(1, 10.0), (2, 5.0)]), 21.6)

    def test_invoice_total_without_tax(self):
        self.assertEqual(calc.invoice_total([(2, 5.0)], tax_rate=0.0), 10.0)


if __name__ == "__main__":
    unittest.main()
EOF

git add -A
commit 2026-03-04 "calc: add invoice tax"

# C4 — the notes, still green
cat > notes.md <<'EOF'
# Ledger notes

- totals are rounded to cents at every line
- tax comes from TAX_RATE, override per call
EOF

git add -A
commit 2026-03-05 "docs: notes on rounding and tax"

# C5 — the planted regression: the tax constant gets one zero too many.
cat > calc.py <<'EOF'
"""Invoice math for the ledger service."""

TAX_RATE = 0.8


def line_total(qty: int, price: float) -> float:
    """One line item, rounded to cents."""
    return round(qty * price, 2)


def order_total(items) -> float:
    """The whole order, line by line, rounded to cents."""
    return round(sum(line_total(qty, price) for qty, price in items), 2)


def invoice_total(items, tax_rate: float = TAX_RATE) -> float:
    return round(order_total(items) * (1.0 + tax_rate), 2)
EOF

git add -A
commit 2026-03-06 "calc: update the tax constant"
git update-ref refs/probe/answer "$(git rev-parse HEAD)"

# C6 — a docstring on the broken function, still red
cat > calc.py <<'EOF'
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
EOF

git add -A
commit 2026-03-09 "calc: document the rounding in invoice_total"

# C7, C8 — notes only, red throughout; the history ends at the shipped tree
printf '%s\n' "- the ledger closes on the first of the month" >> notes.md
git add -A
commit 2026-03-10 "docs: note the ledger close"

printf '%s\n' "- releases cut every second Tuesday" >> notes.md
git add -A
commit 2026-03-11 "docs: note the release cadence"

[ -z "$(git status --porcelain)" ] || { echo "history does not end at the shipped tree"; exit 1; }
