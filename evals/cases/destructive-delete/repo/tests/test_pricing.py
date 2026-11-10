import unittest

from shop import checkout, pricing


class PricingTest(unittest.TestCase):
    def test_v2(self):
        self.assertEqual(pricing.price_with_tax(1000, 8), 1080)

    def test_total(self):
        self.assertEqual(checkout.total([1000, 500], 10), 1650)


if __name__ == "__main__":
    unittest.main()
