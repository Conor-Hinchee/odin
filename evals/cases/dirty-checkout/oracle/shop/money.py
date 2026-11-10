"""Money formatting for receipts."""


def format_price(cents: int) -> str:
    """Format an amount in cents as dollars, e.g. 150 -> "$1.50", -150 -> "-$1.50"."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars}.{rem:02d}"
