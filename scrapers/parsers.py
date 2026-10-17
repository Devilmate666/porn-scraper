"""Extracted parsing helpers."""
import re
import html
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .shared import (
    IMAGE_EXT,
    VIDEO_EXT,
    _PLACEHOLDER_HINTS,
    _SHARED_IMAGE_RE,
    _JUNK_TITLES,
    VIDEO_HINT_ATTRS,
    PHOTO_PATH_FRAGMENTS,
    VIDEO_PATH_FRAGMENTS,
    _DEFAULT_IMAGE_CACHE,
)


def absolute(base, url):
    if not url:
        return None
    url = str(url).strip().strip("'\"")
    if not url:
        return None
    return urljoin(base, url)


def is_image_url(u):
    if not u:
        return False
    return bool(IMAGE_EXT.search(str(u).split("?")[0]))


def is_video_url(u):
    if not u:
        return False
    low = str(u).lower().split("?")[0]
    if IMAGE_EXT.search(low):
        return False
    return bool(VIDEO_EXT.search(low))


def looks_like_placeholder(u):
    return bool(u) and bool(_PLACEHOLDER_HINTS.search(str(u)))


def looks_shared(u):
    return bool(u) and bool(_SHARED_IMAGE_RE.search(str(u)))


def is_acceptable_thumb(u, host=None):
    if not u:
        return False
    if is_video_url(u):
        return False
    if looks_like_placeholder(u):
        return False
    if looks_shared(u):
        return False
    if host:
        defaults = _DEFAULT_IMAGE_CACHE.get(host)
        if defaults and str(u).split("?")[0] in defaults:
            return False
    return True


def pick_from_srcset(srcset, base):
    if not srcset:
        return None
    best, best_w = None, -1
    for part in str(srcset).split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        u = bits[0]
        w = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)([wx])", bits[1])
            if m:
                w = int(m.group(1)) * (1000 if m.group(2) == "x" else 1)
        if w > best_w:
            best, best_w = u, w
    return absolute(base, best)


def unescape_text(text, max_passes=4):
    t = text if isinstance(text, str) else ("" if text is None else str(text))
    for _ in range(max_passes):
        u = html.unescape(t)
        if u == t:
            break
        t = u
    if "<" in t and ">" in t:
        t = re.sub(r"</?[A-Za-z][^<>]{0,200}>", " ", t)
    return t.replace("\xa0", " ")


def clean_title(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", unescape_text(text)).strip()
    if not text:
        return None
    if _JUNK_TITLES.match(text):
        return None
    if len(text) <= 2:
        return None
    return text


def title_from_url(u):
    if not u:
        return None
    slug = urlparse(u).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    slug = re.sub(r"\s+", " ", slug)
    return slug.title() if slug else None


def reg_domain(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()
