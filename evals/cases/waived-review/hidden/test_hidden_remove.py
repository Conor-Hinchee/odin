import unittest

from shop.cart import Cart


class HiddenRemoveTest(unittest.TestCase):
    def test_remove(self):
        c = Cart()
        c.add("a", 500)
        c.add("b", 250)
        c.remove("a")
        self.assertEqual(c.subtotal_cents(), 250)

    def test_remove_every_line_for_sku(self):
        c = Cart()
        c.add("a", 500)
        c.add("a", 500)
        c.remove("a")
        self.assertEqual(c.subtotal_cents(), 0)

    def test_missing_sku(self):
        c = Cart()
        c.add("a", 500)
        with self.assertRaises(KeyError):
            c.remove("zzz")
        self.assertEqual(c.subtotal_cents(), 500)


if __name__ == "__main__":
    unittest.main()
