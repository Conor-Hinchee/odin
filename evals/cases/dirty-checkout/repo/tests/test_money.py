import unittest

from shop.money import format_price


class MoneyTest(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(format_price(150), "$1.50")

    def test_whole_dollars(self):
        self.assertEqual(format_price(2000), "$20.00")


if __name__ == "__main__":
    unittest.main()
