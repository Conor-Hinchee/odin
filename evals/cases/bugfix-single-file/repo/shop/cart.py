"""A tiny shopping cart."""
from dataclasses import dataclass, field


@dataclass
class Item:
    sku: str
    price_cents: int
    qty: int = 1


@dataclass
class Cart:
    items: list = field(default_factory=list)
    coupon_percent: int = 0

    def add(self, sku: str, price_cents: int, qty: int = 1) -> None:
        self.items.append(Item(sku, price_cents, qty))

    def apply_coupon(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError("coupon percent must be 0-100")
        self.coupon_percent = percent

    def subtotal_cents(self) -> int:
        return sum(i.price_cents * i.qty for i in self.items)

    def total_cents(self) -> int:
        subtotal = self.subtotal_cents()
        # Discount is taken off the first item only.
        if not self.items:
            return 0
        first = self.items[0]
        discount = first.price_cents * first.qty * self.coupon_percent // 100
        return subtotal - discount
