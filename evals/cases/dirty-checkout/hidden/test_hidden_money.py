import unittest

from shop.money import format_price


class HiddenMoneyTest(unittest.TestCase):
    def test_refund(self):
        self.assertEqual(format_price(-150), "-$1.50")

    def test_small_refund(self):
        self.assertEqual(format_price(-5), "-$0.05")

    def test_positive_unchanged(self):
        self.assertEqual(format_price(150), "$1.50")
        self.assertEqual(format_price(0), "$0.00")


if __name__ == "__main__":
    unittest.main()
