"""DEPRECATED: v1 pricing with a hard-coded 10% tax. Use shop.pricing instead."""


def price_with_tax(cents: int) -> int:
    return cents + cents // 10
