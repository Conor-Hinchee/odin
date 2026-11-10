"""Sales tax."""
from decimal import ROUND_HALF_UP, Decimal

DEFAULT_RATE = Decimal("8")
RATES = {"CA": Decimal("7.25"), "NY": Decimal("8"), "OR": Decimal("0")}


def tax_cents(subtotal_cents: int, region: str | None = None) -> int:
    rate = RATES[region] if region else DEFAULT_RATE
    return int((Decimal(subtotal_cents) * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
