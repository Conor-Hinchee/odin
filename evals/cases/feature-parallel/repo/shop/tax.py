"""Sales tax."""

DEFAULT_RATE = 0.08


def tax_cents(subtotal_cents: int) -> int:
    return round(subtotal_cents * DEFAULT_RATE)
