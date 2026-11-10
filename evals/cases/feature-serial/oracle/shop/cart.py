"""A tiny shopping cart."""
from dataclasses import dataclass, field

MAX_QTY_PER_SKU = 10
BULK_QTY = 5
BULK_PERCENT = 5


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
        held = sum(i.qty for i in self.items if i.sku == sku)
        if held + qty > MAX_QTY_PER_SKU:
            raise ValueError(f"at most {MAX_QTY_PER_SKU} of {sku} per cart")
        self.items.append(Item(sku, price_cents, qty))

    def apply_coupon(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError("coupon percent must be 0-100")
        self.coupon_percent = percent

    def subtotal_cents(self) -> int:
        return sum(i.price_cents * i.qty for i in self.items)

    def total_cents(self) -> int:
        lines = 0
        for i in self.items:
            line = i.price_cents * i.qty
            if i.qty >= BULK_QTY:
                line -= line * BULK_PERCENT // 100
            lines += line
        return lines - lines * self.coupon_percent // 100
