import unittest
from datetime import datetime, timezone

from shop.report import OrderRecord, todays_report

U = timezone.utc


class HiddenTodayTest(unittest.TestCase):
    # 2026-11-10 06:00 UTC is 2026-11-09 22:00 in Los Angeles: the store's "today" is Nov 9.
    NOW = datetime(2026, 11, 10, 6, 0, tzinfo=U)

    def orders(self):
        return [
            OrderRecord("morning", datetime(2026, 11, 9, 17, 0, tzinfo=U), 100),    # Nov 9, 09:00 store time
            OrderRecord("evening", datetime(2026, 11, 10, 5, 30, tzinfo=U), 200),   # Nov 9, 21:30 store time
            OrderRecord("yesterday", datetime(2026, 11, 9, 7, 0, tzinfo=U), 400),   # Nov 8, 23:00 store time
        ]

    def test_store_local_day(self):
        self.assertEqual(todays_report(self.orders(), now=self.NOW), {"count": 2, "revenue_cents": 300})
