"""The app's in-process cache, shared by every module and every user."""


class Cache:
    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def delete(self, key):
        self._data.pop(key, None)

    def keys(self):
        return list(self._data)

    def clear(self):
        self._data.clear()


cache = Cache()
