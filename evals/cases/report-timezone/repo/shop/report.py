from datetime import datetime, timezone


class OrderRecord:
    def __init__(self, order_id, created_at: datetime, amount_cents: int):
        assert created_at.tzinfo is not None, "created_at must be timezone-aware (UTC)"
        self.order_id = order_id
        self.created_at = created_at
        self.amount_cents = amount_cents


def summarize(orders):
    return {"count": len(orders), "revenue_cents": sum(o.amount_cents for o in orders)}


def utc_now():
    return datetime.now(timezone.utc)
