TAX_RATE_BP = 825  # 8.25%, in basis points


class Receipt:
    def __init__(self):
        self.lines = []  # [(name, unit_cents, qty)]

    def add(self, name, unit_cents, qty=1):
        self.lines.append((name, unit_cents, qty))

    def subtotal_cents(self):
        return sum(unit * qty for _, unit, qty in self.lines)

    def lines_with_tax(self):
        return [(name, (unit * qty * (10000 + TAX_RATE_BP) + 5000) // 10000) for name, unit, qty in self.lines]

    def total_with_tax(self):
        return (self.subtotal_cents() * (10000 + TAX_RATE_BP) + 5000) // 10000
