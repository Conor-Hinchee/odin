from datetime import datetime, timedelta, timezone


class Account:
    def __init__(self, email, last_login_at: datetime, last_order_at: datetime | None = None):
        self.email = email
        self.last_login_at = last_login_at
        self.last_order_at = last_order_at
        self.active = True


def utc_now():
    return datetime.now(timezone.utc)


def deactivate_inactive(accounts, now=None, days=90):
    cutoff = (now or utc_now()) - timedelta(days=days)
    for a in accounts:
        if a.last_login_at < cutoff:
            a.active = False
