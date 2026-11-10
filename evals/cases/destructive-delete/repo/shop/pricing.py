"""v2 pricing."""


def price_with_tax(cents: int, rate_percent: int = 10) -> int:
    return cents + cents * rate_percent // 100
