TAX_RATE_BP = 825  # 8.25%, in basis points


class Receipt:
    def __init__(self):
        self.lines = []  # [(name, unit_cents, qty)]

    def add(self, name, unit_cents, qty=1):
        self.lines.append((name, unit_cents, qty))

    def subtotal_cents(self):
        return sum(unit * qty for _, unit, qty in self.lines)
