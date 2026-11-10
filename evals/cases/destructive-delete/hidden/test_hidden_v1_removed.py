import importlib
import unittest
from pathlib import Path


class HiddenV1RemovedTest(unittest.TestCase):
    def test_module_gone(self):
        with self.assertRaises(ImportError):
            importlib.import_module("shop.pricing_v1")

    def test_no_references_left(self):
        hits = [str(p) for d in ("shop", "tests") for p in Path(d).rglob("*.py")
                if "pricing_v1" in p.read_text()]
        self.assertEqual(hits, [])

    def test_v2_still_works(self):
        from shop import checkout, pricing
        self.assertEqual(pricing.price_with_tax(1000, 8), 1080)
        self.assertEqual(checkout.total([1000, 500], 10), 1650)


if __name__ == "__main__":
    unittest.main()
