TAX_RATE_BP = 825  # 8.25%, in basis points


class Receipt:
    def __init__(self):
        self.lines = []  # [(name, unit_cents, qty)]

    def add(self, name, unit_cents, qty=1):
        self.lines.append((name, unit_cents, qty))

    def subtotal_cents(self):
        return sum(unit * qty for _, unit, qty in self.lines)

    def lines_with_tax(self):
        """Each line's price including tax, rounded half up to the cent."""
        return [(name, (unit * qty * (10000 + TAX_RATE_BP) + 5000) // 10000) for name, unit, qty in self.lines]

    def total_with_tax(self):
        # The sum of the printed lines, so the receipt adds up.
        return sum(cents for _, cents in self.lines_with_tax())
