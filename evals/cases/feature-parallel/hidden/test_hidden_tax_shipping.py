import unittest

from shop.shipping import shipping_cents
from shop.tax import tax_cents


class HiddenTaxTest(unittest.TestCase):
    def test_regions(self):
        self.assertEqual(tax_cents(1000, "CA"), 73)   # 72.5 rounds half up
        self.assertEqual(tax_cents(999, "CA"), 72)
        self.assertEqual(tax_cents(1000, "NY"), 80)
        self.assertEqual(tax_cents(1000, "OR"), 0)

    def test_default(self):
        self.assertEqual(tax_cents(1000), 80)
        self.assertEqual(tax_cents(1000, region=None), 80)


class HiddenShippingTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(shipping_cents(0), 500)
        self.assertEqual(shipping_cents(1000), 500)
        self.assertEqual(shipping_cents(1001), 650)
        self.assertEqual(shipping_cents(1500), 650)
        self.assertEqual(shipping_cents(1501), 800)
        self.assertEqual(shipping_cents(3000), 1100)


if __name__ == "__main__":
    unittest.main()
