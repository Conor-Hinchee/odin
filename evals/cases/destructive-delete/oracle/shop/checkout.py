from shop import pricing


def total(line_cents: list, rate_percent: int = 10) -> int:
    return sum(pricing.price_with_tax(c, rate_percent) for c in line_cents)
