import unittest

from shop.cart import Cart


class HiddenStackingTest(unittest.TestCase):
    def cart(self, *coupons):
        c = Cart()
        c.add("mug", 10000)
        for p in coupons:
            c.apply_coupon(p)
        return c

    def test_one_coupon_unchanged(self):
        self.assertEqual(self.cart(10).total_cents(), 9000)

    def test_stacked_coupons_apply_in_turn(self):
        self.assertEqual(self.cart(10, 20).total_cents(), 7200)  # 10% off, then 20% off what's left

    def test_three(self):
        self.assertEqual(self.cart(50, 50, 50).total_cents(), 1250)
