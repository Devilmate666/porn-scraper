"""Video card extraction - isolated module."""
import re
import hashlib
from collections import Counter
from urllib.parse import urljoin, urlparse

from .shared import (
    VIDEO_HINT_ATTRS,
    PHOTO_PATH_FRAGMENTS,
    VIDEO_PATH_FRAGMENTS,
    _META_SKIP_TEXT,
    _META_TYPES,
    _DUR_RE,
    _NUM_RE,
    _PCT_RE,
    _DEFAULT_IMAGE_CACHE,
)
from .parsers import (
    absolute,
    is_image_url,
    is_video_url,
    looks_like_placeholder,
    is_acceptable_thumb,
    pick_from_srcset,
    clean_title,
    title_from_url,
    reg_domain,
)


def _find_thumbnail(container, base, host=None):
    def _try(u):
        if u and is_acceptable_thumb(u, host) and not looks_like_placeholder(u):
            return u
        return None

    for v in container.find_all("video"):
        for attr in ("poster", "data-poster", "data-video-poster"):
            hit = _try(absolute(base, v.get(attr)))
            if hit:
                return hit

    for img in container.find_all("img"):
        for attr in ("data-src", "data-original", "data-lazy", "data-lazy-src",
                     "data-thumb", "data-thumbnail", "data-image", "data-cover",
                     "data-poster", "data-preview", "data-webp", "data-src-retina"):
            hit = _try(absolute(base, img.get(attr)))
            if hit:
                return hit
        for ss_attr in ("data-srcset", "srcset"):
            hit = _try(pick_from_srcset(img.get(ss_attr), base))
            if hit:
                return hit
        hit = _try(absolute(base, img.get("src")))
        if hit:
            return hit

    for s in container.find_all("source"):
        for ss_attr in ("data-srcset", "srcset"):
            hit = _try(pick_from_srcset(s.get(ss_attr), base))
            if hit:
                return hit
        hit = _try(absolute(base, s.get("data-src") or s.get("src")))
        if hit:
            return hit

    for el in [container, *container.find_all(style=True)]:
        style = el.get("style") or ""
        m = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", style)
        if m:
            hit = _try(absolute(base, m.group(1)))
            if hit:
                return hit
    for el in [container, *container.find_all(True)]:
        for attr in ("data-bg", "data-background", "data-bg-src", "data-cover"):
            hit = _try(absolute(base, el.get(attr)))
            if hit:
                return hit
    return None


def _find_video_source(container, base):
    for v in container.find_all("video"):
        src = v.get("src") or v.get("data-src")
        if is_video_url(src):
            return absolute(base, src)
        for s in v.find_all("source"):
            ss = s.get("src") or s.get("data-src")
            if is_video_url(ss):
                return absolute(base, ss)
    return None


def _find_title(container, base):
    for attr in ("title", "aria-label", "data-title",
                 "data-name", "data-video-title", "data-original-title",
                 "data-tooltip", "data-label"):
        t = clean_title(container.get(attr))
        if t:
            return t
    for tag in ("h1", "h2", "h3", "h4", "h5"):
        h = container.find(tag)
        if h:
            t = clean_title(h.get_text(strip=True))
            if t:
                return t
    img = container.find("img")
    if img and img.get("alt"):
        alt = str(img["alt"]).strip()
        if alt and not looks_like_placeholder(alt):
            t = clean_title(alt)
            if t:
                return t
    for cls in ("title", "name", "video-title", "card-title",
                "entry-title", "post-title", "item-title"):
        el = container.find(class_=lambda c: c and cls in c)
        if el:
            t = clean_title(el.get_text(strip=True))
            if t:
                return t
    for el in container.find_all(["span", "div", "p", "strong", "b"]):
        t = clean_title(el.get_text(strip=True))
        if t and len(t) > 3 and len(t) < 300 and not _JUNK_TITLES.match(t):
            return t
    return None


def _card_has_video_hint(container):
    if container.find("video") is not None:
        return True
    for el in [container, *container.find_all(True)]:
        for attr in VIDEO_HINT_ATTRS:
            if el.get(attr):
                return True
    return False


def _url_looks_like_photo(path):
    low = str(path).lower()
    return any(frag in low for frag in PHOTO_PATH_FRAGMENTS)


def _url_looks_like_video(path):
    low = str(path).lower()
    if any(frag in low for frag in VIDEO_PATH_FRAGMENTS):
        return True
    if VIDEO_EXT.search(low):
        return True
    return False


def _is_video_preview(a, base_host):
    href = a.get("href", "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    full = urljoin("http://" + base_host, href) if not href.startswith("http") else href
    parsed = urlparse(full)
    if base_host and base_host not in parsed.netloc:
        return False
    path = parsed.path or "/"
    if _url_looks_like_photo(path):
        return False
    for skip in ("/login", "/signup", "/register", "/terms", "/privacy", "/contact", "/about", "/faq", "/dmca"):
        if path.lower().startswith(skip):
            return False
    if re.match(r"^/videos(/(page/)?\d+)?/?$", path.lower()):
        return False
    cls_id = " ".join(list(a.get("class") or []) + [a.get("id") or ""]).lower()
    if any(k in cls_id for k in ("navbar", "nav-", "-nav", "menu", "header", "footer", "breadcrumb", "pagination")):
        return False
    if _card_has_video_hint(a):
        return True
    if _url_looks_like_video(path):
        return True
    return False