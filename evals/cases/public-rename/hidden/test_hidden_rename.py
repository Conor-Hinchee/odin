import json
import unittest

from shop.order import Order
from shop import webhook


class HiddenRenameTest(unittest.TestCase):
    def setUp(self):
        self.order = Order("o1", [("mug", 1200, 2), ("tee", 2500, 1)])

    def test_renamed_in_code(self):
        self.assertEqual(self.order.subtotal, 4900)

    def test_webhook_contract_keeps_total_key(self):
        body = json.loads(webhook.payload(self.order))
        self.assertEqual(body["total"], 4900)
