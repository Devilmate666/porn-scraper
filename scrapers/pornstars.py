"""Unified pornstars/models scraper with improved stability."""
import hashlib
import re
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .shared import (
    _COUNT_TAIL,
    _SP_STAR_PATH,
    _PVB_ACTOR_SECTIONS,
    _PVB_STAR_QS,
    _PVB_NOT_A_STAR,
    _PVB_ALT_PREFIX,
    _MODEL_HREF_RE,
    JUNK_TITLES,
)
from .parsers import (
    absolute,
    is_image_url,
    looks_like_placeholder,
    clean_title,
    title_from_url,
    reg_domain,
)
from .cards import (
    _find_thumbnail,
    _find_title,
    title_from_url,
    _find_video_source,
)
from cache import LRUCache

# Caching for pornstars
pornstars_cache = LRUCache(maxsize=512, ttl=600)


def _clean_category_name(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"[\s\n]*[\d,]+\s*(?:videos?|vids?|clips?)?\s*$", "", text, flags=re.I).strip()
    if not text or len(text) < 2 or len(text) > 80:
        return None
    if re.match(r"^\d+$", text):
        return None
    if JUNK_TITLES.match(text):
        return None
    if re.search(r"\blogo\b", text, re.I):
        return None
    return text


def _clean_video_count(text):
    m = re.search(r"([\d,]+)", str(text) if text else "")
    return int(m.group(1).replace(",", "")) if m else None


def _img_url(img, base):
    for attr in ("data-src", "data-original", "data-lazy-src", "data-webp",
                 "data-thumb", "data-img", "data-savepage-src", "src"):
        v = (img.get(attr) or "").strip() if img else ""
        if v and not v.startswith("data:"):
            return absolute(base, v)
    for attr in ("data-srcset", "srcset"):
        v = img.get(attr) if img else ""
        if v:
            from .parsers import pick_from_srcset
            got = pick_from_srcset(v, base)
            if got:
                return got
    return None


def _collapse_repeat(name):
    w = (name or "").split()
    n = len(w)
    if n >= 2 and n % 2 == 0:
        first = [x.lower() for x in w[:n // 2]]
        second = [x.lower() for x in w[n // 2:]]
        if first == second:
            return " ".join(w[:n // 2])
    return name


def extract_models(soup, base, max_items=200):
    """Extract model/pornstar entries from freesexvideos-style pages."""
    host = urlparse(base).netloc
    out, seen = [], set()
    root = (soup.select_one("#list_models_models_list_items")
            or soup.select_one("[id^='list_models']")
            or soup.select_one(".list-models")
            or soup.body or soup)
    for a in root.find_all("a", href=True):
        href = a.get("href") or ""
        link = urljoin(base, href.split("#")[0])
        p = urlparse(link)
        if host and p.netloc != host:
            continue
        mm = _MODEL_HREF_RE.search(p.path)
        if not mm or mm.group(1).isdigit():
            continue
        key = link.split("?")[0].rstrip("/")
        if key in seen:
            continue
        card = a
        for _ in range(3):
            if card.find("img") and (card.name != "a" or card.get_text(strip=True)):
                break
            if card.parent is None or getattr(card.parent, 'name', '') in ("body", "html", "[document]"):
                break
            card = card.parent
        img = a.find("img") or card.find("img")
        title_el = card.select_one(".title, strong, h2, h3")
        name = _clean_category_name(
            (a.get("title") or "")
            or (title_el.get_text(" ", strip=True) if title_el else "")
            or (img.get("alt") if img else "")
            or title_from_url(link))
        if not name:
            continue
        ctext = card.get_text(" ", strip=True)
        cm = re.search(r"([\d][\d,]*)\s*(?:videos?|vids?|clips?)", ctext, re.I)
        seen.add(key)
        out.append({
            "name": name,
            "link": link,
            "thumbnail": _img_url(img, base),
            "video_count": _clean_video_count(cm.group(1)) if cm else None,
            "uid": hashlib.md5(link.encode("utf-8")).hexdigest()[:12],
        })
        if len(out) >= max_items:
            break
    return out


def extract_superporn_pornstars(soup, base, max_items=200):
    """Extract pornstars from superporn.com."""
    host = urlparse(base).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        link = urljoin(base, href.split("#")[0])
        p = urlparse(link)
        if host and p.netloc != host:
            continue
        if not _SP_STAR_PATH.match(p.path or ""):
            continue
        key = link.split("?")[0].rstrip("/")
        if key in seen:
            continue
        txt = a.get_text(" ", strip=True)
        m = re.search(r"([\d][\d,]*)\s*(?:videos?|vids?|clips?)", txt, re.I)
        img = a.find("img")
        name = _clean_category_name(_collapse_repeat((txt[:m.start()] if m else txt).strip())) \
            or _clean_category_name(img.get("alt") if img else None) \
            or _clean_category_name(title_from_url(link))
        if not name:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "link": link,
            "thumbnail": _img_url(img, base) or _find_thumbnail(a, base, host=host),
            "video_count": _clean_video_count(m.group(1)) if m else None,
            "uid": hashlib.md5(link.encode("utf-8")).hexdigest()[:12],
        })
        if len(out) >= max_items:
            break
    return out


def extract_pornvideobb_actors(soup, base, max_items=300):
    """Extract actors/stars from pornvideobb."""
    host = urlparse(base).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        link = urljoin(base, href.split("#")[0])
        p = urlparse(link)
        if host and p.netloc != host:
            continue
        segs = [x for x in (p.path or "/").split("/") if x]
        first = segs[0].lower() if segs else ""
        is_actor = bool(_PVB_ACTOR_SECTIONS.match(first))
        qs = dict(parse_qsl(p.query)) if hasattr(p, 'query') else {}
        qs_name = next((qs[k] for k in qs if k.lower() in _PVB_STAR_QS and qs[k]), "").strip()
        by_path = is_actor and len(segs) >= 2 and not _PVB_NOT_A_STAR.match(segs[1])
        by_query = (first == "zone_video" or is_actor) and bool(qs_name)
        if not (by_path or by_query):
            continue
        key = (p.path.lower(), qs_name.lower()) if qs_name else link
        if key in seen:
            continue
        txt = a.get_text(" ", strip=True)
        m = re.search(r"([\d][\d,]*)\s*(?:videos?|vids?|clips?)", txt, re.I)
        img = a.find("img")
        alt = _PVB_ALT_PREFIX.sub("", (img.get("alt") if img else "") or "")
        slug = segs[1].replace("-", " ").replace("_", " ") if len(segs) > 1 else ""
        name = _clean_category_name(qs_name) \
            or _clean_category_name(a.get("title")) \
            or _clean_category_name(txt[:m.start()] if m else txt) \
            or _clean_category_name(alt) \
            or _clean_category_name(slug)
        if not name:
            continue
        seen.add(key)
        thumb = _img_url(img, base) or _find_thumbnail(a, base, host=host)
        out.append({
            "name": _collapse_repeat(name),
            "link": link,
            "thumbnail": thumb.replace(" ", "%20") if thumb and isinstance(thumb, str) else None,
            "video_count": _clean_video_count(m.group(1)) if m else None,
            "uid": hashlib.md5(link.encode("utf-8")).hexdigest()[:12],
        })
        if len(out) >= max_items:
            break
    return out
