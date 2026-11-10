import unittest

from shop.receipt import Receipt


class ReceiptTest(unittest.TestCase):
    def test_subtotal(self):
        r = Receipt()
        r.add("mug", 1200, 2)
        self.assertEqual(r.subtotal_cents(), 2400)


if __name__ == "__main__":
    unittest.main()
