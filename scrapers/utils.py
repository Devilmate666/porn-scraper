"""Extracted utility functions for scrapers."""
from collections import Counter
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .parsers import (
    absolute,
    is_image_url,
    is_video_url,
    looks_like_placeholder,
    is_acceptable_thumb,
    pick_from_srcset,
    reg_domain,
    clean_title,
)
from .shared import (
    IMAGE_EXT,
    _DEFAULT_IMAGE_CACHE,
)


def _record_default_images(host, soup):
    if not host:
        return
    counts = Counter()
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy",
                     "data-thumb", "data-poster"):
            v = img.get(attr)
            if v and not looks_like_placeholder(v):
                counts[str(v).split("?")[0]] += 1
    for v in soup.find_all("video"):
        p = v.get("poster")
        if p and not looks_like_placeholder(p):
            counts[str(p).split("?")[0]] += 1
    defaults = _DEFAULT_IMAGE_CACHE.setdefault(host, set())
    for url, n in counts.items():
        if n >= 3 or False:  # simplified - _looks_shared checked via reg
            defaults.add(url)


# Forward declarations - these will be defined in scraper or imported
def fetch_html(*args, **kwargs):
    from scraper import fetch_html as _fetch_html
    return _fetch_html(*args, **kwargs)


def _find_next_page_link(*args, **kwargs):
    from scraper import _find_next_page_link as _fn
    return _fn(*args, **kwargs)


def _current_page_number(*args, **kwargs):
    from scraper import _current_page_number as _fn
    return _fn(*args, **kwargs)


def _guess_next_page(*args, **kwargs):
    from scraper import _guess_next_page as _fn
    return _fn(*args, **kwargs)


def _find_thumbnail(*args, **kwargs):
    from .cards import _find_thumbnail as _fn
    return _fn(*args, **kwargs)
