"""Shipping cost."""


def shipping_cents(weight_grams: int) -> int:
    extra = max(0, weight_grams - 1000)
    return 500 + 150 * -(-extra // 500)
