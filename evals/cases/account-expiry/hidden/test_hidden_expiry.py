import unittest
from datetime import datetime, timedelta, timezone

from shop.accounts import Account, deactivate_inactive

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def days_ago(n):
    return NOW - timedelta(days=n)


class HiddenExpiryTest(unittest.TestCase):
    def test_long_idle_is_deactivated(self):
        a = Account("idle@example.com", days_ago(200), days_ago(200))
        deactivate_inactive([a], now=NOW)
        self.assertFalse(a.active)

    def test_recent_login_stays(self):
        a = Account("login@example.com", days_ago(10), None)
        deactivate_inactive([a], now=NOW)
        self.assertTrue(a.active)

    def test_recent_order_counts_as_activity(self):
        a = Account("app@example.com", days_ago(300), days_ago(5))  # orders through a saved app session
        deactivate_inactive([a], now=NOW)
        self.assertTrue(a.active)
