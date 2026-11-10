import unittest

from shop.shipping import shipping_cents
from shop.tax import tax_cents


class BasicsTest(unittest.TestCase):
    def test_default_tax(self):
        self.assertEqual(tax_cents(1000), 80)

    def test_light_parcel(self):
        self.assertEqual(shipping_cents(200), 500)


if __name__ == "__main__":
    unittest.main()
