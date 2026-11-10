class Cart:
    def __init__(self):
        self.lines = []  # [(sku, unit_cents, qty)]
        self.coupons = []

    def add(self, sku, unit_cents, qty=1):
        self.lines.append((sku, unit_cents, qty))

    def apply_coupon(self, percent):
        self.coupons.append(percent)

    def subtotal_cents(self):
        return sum(unit * qty for _, unit, qty in self.lines)

    def total_cents(self):
        sub = self.subtotal_cents()
        percent = min(sum(self.coupons), 100)
        return sub - sub * percent // 100
