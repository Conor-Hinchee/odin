import unittest

from shop.cart import Cart


class CartTest(unittest.TestCase):
    def test_subtotal(self):
        c = Cart()
        c.add("mug", 1200, 2)
        self.assertEqual(c.subtotal_cents(), 2400)

    def test_one_coupon(self):
        c = Cart()
        c.add("mug", 10000)
        c.apply_coupon(10)
        self.assertEqual(c.total_cents(), 9000)


if __name__ == "__main__":
    unittest.main()
