import json


class Order:
    def __init__(self, order_id, lines):
        self.order_id = order_id
        self.lines = lines  # [(sku, unit_cents, qty)]

    @property
    def subtotal(self):
        return sum(unit * qty for _, unit, qty in self.lines)

    def to_json(self):
        return json.dumps({
            "id": self.order_id,
            "subtotal": self.subtotal,
            "lines": [{"sku": s, "unit_cents": u, "qty": q} for s, u, q in self.lines],
        })
