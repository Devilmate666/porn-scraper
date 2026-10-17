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
    
    def get(self, key, default=None):
        with self._lock:
            if key not in self._cache:
                return default
            value, timestamp = self._cache[key]
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                return default
            self._cache.move_to_end(key)
            return value
    
    def set(self, key, value):
        with self._lock:
            self._cache[key] = (value, time.time())
            self._cache.move_to_end(key)
            if len(self._cache) > self.maxsize:
                self._cache.popitem(last=False)
    
    def __setitem__(self, key, value):
        self.set(key, value)
    
    def __getitem__(self, key):
        with self._lock:
            if key not in self._cache:
                raise KeyError(key)
            value, timestamp = self._cache[key]
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                raise KeyError(key)
            self._cache.move_to_end(key)
            return value
    
    def __contains__(self, key):
        val = self.get(key, None)
        return val is not None
    
    def __delitem__(self, key):
        with self._lock:
            if key in self._cache:
                del self._cache[key]
    
    def pop(self, key, default=None):
        with self._lock:
            if key not in self._cache:
                return default
            value, timestamp = self._cache[key]
            del self._cache[key]
            return value
    
    def items(self):
        with self._lock:
            now = time.time()
            result = []
            for k, (v, ts) in list(self._cache.items()):
                if now - ts <= self.ttl:
                    result.append((k, (v, ts)))
            return [(k, v) for k, (v, ts) in result]
    
    def keys(self):
        return [k for k, v in self.items()]
    
    def clear(self):
        with self._lock:
            self._cache.clear()
    
    def __len__(self):
        with self._lock:
            now = time.time()
            count = 0
            for _, (_, ts) in self._cache.items():
                if now - ts <= self.ttl:
                    count += 1
            return count


# Global caches
resolve_cache = LRUCache(maxsize=4096, ttl=600)
tags_cache = LRUCache(maxsize=2048, ttl=600)
studios_cache = LRUCache(maxsize=1024, ttl=600)
search_cache = LRUCache(maxsize=1024, ttl=300)
tags_ttl_cache = LRUCache(maxsize=2048, ttl=600)
page_cache = LRUCache(maxsize=600, ttl=600)
meta_cache = LRUCache(maxsize=400, ttl=900)
