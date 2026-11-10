from shop import pricing, pricing_v1


def total(line_cents: list, rate_percent: int = 10) -> int:
    return sum(pricing.price_with_tax(c, rate_percent) for c in line_cents)


def legacy_total(line_cents: list) -> int:
    """Old checkout path, kept for the v1 mobile app."""
    return sum(pricing_v1.price_with_tax(c) for c in line_cents)
