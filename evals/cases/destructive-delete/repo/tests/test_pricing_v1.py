import unittest

from shop import checkout, pricing_v1


class PricingV1Test(unittest.TestCase):
    def test_v1(self):
        self.assertEqual(pricing_v1.price_with_tax(1000), 1100)

    def test_legacy_total(self):
        self.assertEqual(checkout.legacy_total([1000]), 1100)


if __name__ == "__main__":
    unittest.main()
