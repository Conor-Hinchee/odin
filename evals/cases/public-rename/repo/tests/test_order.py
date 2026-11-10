import json
import unittest

from shop.order import Order
from shop import webhook


class OrderTest(unittest.TestCase):
    def test_amount(self):
        self.assertEqual(Order("o1", [("mug", 1200, 2)]).total, 2400)

    def test_payload_is_json(self):
        body = json.loads(webhook.payload(Order("o1", [("mug", 1200, 1)])))
        self.assertEqual(body["id"], "o1")

    def test_summary(self):
        self.assertEqual(webhook.summary_line(Order("o1", [("mug", 1200, 1)])), "Order o1: 1200 cents")


if __name__ == "__main__":
    unittest.main()
