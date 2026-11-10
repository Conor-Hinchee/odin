from shop.cache import cache


def login(user_id, name):
    cache.set(f"user:{user_id}:profile", {"id": user_id, "name": name})
    cache.set(f"user:{user_id}:cart", [])
    return f"token-{user_id}"


def add_to_cart(user_id, sku):
    cart = cache.get(f"user:{user_id}:cart", [])
    cart.append(sku)
    cache.set(f"user:{user_id}:cart", cart)


def logout(user_id):
    prefix = f"user:{user_id}:"
    for key in cache.keys():
        if key.startswith(prefix):
            cache.delete(key)
