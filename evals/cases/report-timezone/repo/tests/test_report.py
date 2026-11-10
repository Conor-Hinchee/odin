import unittest
from datetime import datetime, timezone

from shop.report import OrderRecord, summarize


class ReportTest(unittest.TestCase):
    def test_summarize(self):
        o = [OrderRecord("a", datetime(2026, 11, 9, 12, tzinfo=timezone.utc), 500),
             OrderRecord("b", datetime(2026, 11, 9, 13, tzinfo=timezone.utc), 700)]
        self.assertEqual(summarize(o), {"count": 2, "revenue_cents": 1200})


if __name__ == "__main__":
    unittest.main()
