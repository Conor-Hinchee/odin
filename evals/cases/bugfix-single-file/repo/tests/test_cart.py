import unittest

from shop.cart import Cart


class CartTest(unittest.TestCase):
    def test_subtotal(self):
        c = Cart()
        c.add("a", 500, 2)
        c.add("b", 250)
        self.assertEqual(c.subtotal_cents(), 1250)

    def test_total_without_coupon(self):
        c = Cart()
        c.add("a", 500)
        self.assertEqual(c.total_cents(), 500)

    def test_bad_coupon(self):
        with self.assertRaises(ValueError):
            Cart().apply_coupon(120)


if __name__ == "__main__":
    unittest.main()
