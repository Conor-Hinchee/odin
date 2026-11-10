import unittest
from datetime import datetime, timezone

from shop.accounts import Account


class AccountTest(unittest.TestCase):
    def test_new_account_is_active(self):
        a = Account("a@example.com", datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertTrue(a.active)


if __name__ == "__main__":
    unittest.main()
