"""Improved caching module for all scraper components."""

# Re-export from cache.utils for compatibility
from cache.utils import (
    LRUCache,
    resolve_cache,
    tags_cache,
    studios_cache,
    search_cache,
    tags_ttl_cache,
    page_cache,
    meta_cache,
)
