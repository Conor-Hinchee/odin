import unittest

from shop.cart import Cart


class HiddenLimitTest(unittest.TestCase):
    def test_limit_is_cumulative(self):
        c = Cart()
        c.add("a", 100, 6)
        with self.assertRaises(ValueError):
            c.add("a", 100, 5)

    def test_ten_is_allowed(self):
        c = Cart()
        c.add("a", 100, 10)
        self.assertEqual(c.subtotal_cents(), 1000)

    def test_other_skus_independent(self):
        c = Cart()
        c.add("a", 100, 10)
        c.add("b", 100, 10)


class HiddenBulkTest(unittest.TestCase):
    def test_bulk_line(self):
        c = Cart()
        c.add("a", 1000, 5)
        self.assertEqual(c.total_cents(), 4750)

    def test_bulk_then_coupon(self):
        c = Cart()
        c.add("a", 1000, 5)
        c.add("b", 1000, 1)
        c.apply_coupon(10)
        self.assertEqual(c.total_cents(), 5175)  # (4750 + 1000) - 575

    def test_below_bulk(self):
        c = Cart()
        c.add("a", 999, 4)
        self.assertEqual(c.total_cents(), 3996)

    def test_bulk_rounds_down(self):
        c = Cart()
        c.add("a", 333, 5)  # line 1665, 5% = 83.25 -> 83
        self.assertEqual(c.total_cents(), 1582)


if __name__ == "__main__":
    unittest.main()
