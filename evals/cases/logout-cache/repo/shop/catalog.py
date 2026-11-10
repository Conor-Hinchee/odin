from shop.cache import cache

PRODUCTS = {"mug": {"name": "Mug", "price_cents": 1200}, "tee": {"name": "Tee", "price_cents": 2500}}


def get_product(sku):
    key = f"product:{sku}"
    hit = cache.get(key)
    if hit is None:
        hit = dict(PRODUCTS[sku])
        cache.set(key, hit)
    return hit
