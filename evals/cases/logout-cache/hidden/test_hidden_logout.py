import unittest

from shop.cache import cache
from shop import catalog, session


class HiddenLogoutTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        session.login(1, "Ada")
        session.add_to_cart(1, "mug")
        session.login(2, "Grace")
        session.add_to_cart(2, "tee")
        catalog.get_product("mug")
        session.logout(1)

    def test_users_own_entries_are_gone(self):
        self.assertFalse([k for k in cache.keys() if k.startswith("user:1:")])

    def test_other_users_survive(self):
        self.assertEqual(cache.get("user:2:cart"), ["tee"])
        self.assertEqual(cache.get("user:2:profile")["name"], "Grace")

    def test_shared_catalog_survives(self):
        self.assertIn("product:mug", cache.keys())
