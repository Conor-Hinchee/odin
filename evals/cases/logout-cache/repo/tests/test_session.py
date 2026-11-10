import unittest

from shop.cache import cache
from shop import catalog, session


class SessionTest(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_login_caches_profile(self):
        session.login(1, "Ada")
        self.assertEqual(cache.get("user:1:profile")["name"], "Ada")

    def test_cart(self):
        session.login(1, "Ada")
        session.add_to_cart(1, "mug")
        self.assertEqual(cache.get("user:1:cart"), ["mug"])

    def test_catalog_is_cached(self):
        catalog.get_product("mug")
        self.assertIn("product:mug", cache.keys())


if __name__ == "__main__":
    unittest.main()
