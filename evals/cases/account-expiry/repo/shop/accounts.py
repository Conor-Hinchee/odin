from datetime import datetime, timezone


class Account:
    def __init__(self, email, last_login_at: datetime, last_order_at: datetime | None = None):
        self.email = email
        self.last_login_at = last_login_at
        self.last_order_at = last_order_at
        self.active = True


def utc_now():
    return datetime.now(timezone.utc)
