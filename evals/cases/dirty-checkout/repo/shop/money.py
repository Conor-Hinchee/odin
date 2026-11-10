"""Money formatting for receipts."""


def format_price(cents: int) -> str:
    """Format an amount in cents as dollars, e.g. 150 -> "$1.50"."""
    dollars, rem = divmod(cents, 100)
    return f"${dollars}.{rem:02d}"
