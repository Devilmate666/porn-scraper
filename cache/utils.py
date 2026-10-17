"""Caching utilities with TTL and size limits."""
import time
from collections import OrderedDict
from threading import Lock


class LRUCache:
    """Simple LRU cache with TTL support."""
    
    def __init__(self, maxsize=1024, ttl=600):
        self.maxsize = maxsize
        self.ttl = ttl
        self._cache = OrderedDict()
        self._lock = Lock()
    
    def get(self, key):
        with self._lock:
            if key not in self._cache:
                return None
            value, timestamp = self._cache[key]
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                return None
            # Move to end (recently used)
            self._cache.move_to_end(key)
            return value
    
    def set(self, key, value):
        with self._lock:
            self._cache[key] = (value, time.time())
            self._cache.move_to_end(key)
            if len(self._cache) > self.maxsize:
                # Remove oldest
                self._cache.popitem(last=False)
    
    def clear(self):
        with self._lock:
            self._cache.clear()
    
    def __len__(self):
        return len(self._cache)


# Global caches
resolve_cache = LRUCache(maxsize=4096, ttl=600)
tags_cache = LRUCache(maxsize=2048, ttl=600)
studios_cache = LRUCache(maxsize=1024, ttl=600)
search_cache = LRUCache(maxsize=1024, ttl=300)
tags_ttl_cache = LRUCache(maxsize=2048, ttl=600)  # for tags by URL
page_cache = LRUCache(maxsize=600, ttl=600)
meta_cache = LRUCache(maxsize=400, ttl=900)
