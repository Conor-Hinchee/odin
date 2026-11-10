import unittest

from shop.cart import Cart


class HiddenCouponTest(unittest.TestCase):
    def test_coupon_applies_to_whole_cart(self):
        c = Cart()
        c.add("a", 1000)
        c.add("b", 1000)
        c.apply_coupon(10)
        self.assertEqual(c.total_cents(), 1800)

    def test_single_item_coupon(self):
        c = Cart()
        c.add("a", 999, 3)
        c.apply_coupon(50)
        self.assertEqual(c.total_cents(), 2997 - 1498)

    def test_empty_cart(self):
        c = Cart()
        c.apply_coupon(25)
        self.assertEqual(c.total_cents(), 0)

    def test_existing_behaviour_kept(self):
        c = Cart()
        c.add("a", 500, 2)
        self.assertEqual(c.total_cents(), 1000)


if __name__ == "__main__":
    unittest.main()
