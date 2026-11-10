class Cart:
    def __init__(self):
        self.lines = []  # [(sku, unit_cents, qty)]
        self.coupon_percent = 0

    def add(self, sku, unit_cents, qty=1):
        self.lines.append((sku, unit_cents, qty))

    def apply_coupon(self, percent):
        self.coupon_percent = percent

    def subtotal_cents(self):
        return sum(unit * qty for _, unit, qty in self.lines)

    def total_cents(self):
        sub = self.subtotal_cents()
        return sub - sub * self.coupon_percent // 100
