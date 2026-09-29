"""Extract video preview elements from a site's homepage + resolve detail page to real stream."""

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from urllib.parse import (
    urljoin, urlparse, quote_plus, urlsplit, urlunsplit,
    parse_qsl, urlencode, unquote,
)

import httpx
from bs4 import BeautifulSoup


# ----------------------------------------------------------------
# Networking helpers
# ----------------------------------------------------------------
import socket as _socket
import ipaddress as _ipaddress
import urllib.request as _urlreq

_orig_getaddrinfo = _socket.getaddrinfo
_doh_cache = {}
_dns_diag = {}      # host -> short explanation of why the fallback resolvers failed

def _udp_dns_lookup(host, servers=("1.1.1.1", "8.8.8.8", "9.9.9.9"), port=53):
    """Minimal stdlib UDP DNS client (A records). Returns (ips, notes)."""
    import struct, random
    qid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    try:
        for label in host.rstrip(".").split("."):
            b = label.encode("idna")
            q += bytes([len(b)]) + b
    except Exception as e:
        return [], [f"bad hostname: {e}"]
    q += b"\x00" + struct.pack(">HH", 1, 1)

    def skip_name(d, pos):
        while True:
            n = d[pos]
            if n == 0:
                return pos + 1
            if n & 0xC0 == 0xC0:
                return pos + 2
            pos += 1 + n

    notes = []
    for srv in servers:
        sock = _orig_socket_cls(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sock.settimeout(3)
            sock.sendto(q, (srv, port))
            data, _ = sock.recvfrom(4096)
        except Exception as e:
            notes.append(f"UDP {srv}: {e}")
            continue
        finally:
            sock.close()
        try:
            if data[:2] != q[:2]:
                notes.append(f"UDP {srv}: mismatched reply")
                continue
            rcode = data[3] & 0x0F
            if rcode == 3:
                notes.append(f"UDP {srv}: NXDOMAIN (host does not exist)")
                return [], notes
            qd, an = struct.unpack(">HH", data[4:8])
            pos = 12
            for _ in range(qd):
                pos = skip_name(data, pos) + 4
            ips = []
            for _ in range(an):
                pos = skip_name(data, pos)
                rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
                pos += 10
                if rtype == 1 and rdlen == 4:
                    ips.append(".".join(str(b) for b in data[pos:pos + 4]))
                pos += rdlen
            if ips:
                return ips, notes
            notes.append(f"UDP {srv}: no A records")
        except Exception as e:
            notes.append(f"UDP {srv}: parse error {e}")
    return [], notes

_orig_socket_cls = _socket.socket

def _doh_lookup(host):
    """Fallback resolution when the system resolver fails (ISP/VPN DNS blocking or flaky DNS):
    DNS-over-HTTPS first, then plain UDP DNS to public resolvers. Records why it failed."""
    if host in _doh_cache and _doh_cache[host]:
        return _doh_cache[host]
    ips, notes = [], []
    for ep in ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve"):
        try:
            req = _urlreq.Request(f"{ep}?name={host}&type=A",
                                  headers={"Accept": "application/dns-json"})
            with _urlreq.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            if data.get("Status") == 3:
                notes.append(f"DoH {ep.split('/')[2]}: NXDOMAIN (host does not exist)")
                break
            ips = [a["data"] for a in data.get("Answer", [])
                   if a.get("type") == 1 and a.get("data")]
            if ips:
                break
            notes.append(f"DoH {ep.split('/')[2]}: no A records")
        except Exception as e:
            notes.append(f"DoH {ep.split('/')[2]}: {e}")
    if not ips and not any("NXDOMAIN" in n for n in notes):
        ips, n2 = _udp_dns_lookup(host)
        notes += n2
    _doh_cache[host] = ips
    _dns_diag[host] = "; ".join(notes) if not ips else ""
    if not ips:
        print(f"[dns] fallback could not resolve {host}: {_dns_diag[host]}")
    return ips

def _v4_first(results):
    # Signed video links are bound to the requesting IP, so scraper and proxy should reach the
    # site the same way. Try IPv4 first; IPv6 stays available as a fallback.
    return sorted(results, key=lambda r: 0 if r[0] == _socket.AF_INET else 1)

def _getaddrinfo_with_fallback(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _v4_first(_orig_getaddrinfo(host, port, family, type, proto, flags))
    except _socket.gaierror:
        if not isinstance(host, str):
            raise
        try:
            _ipaddress.ip_address(host)
            raise            # already an IP literal; nothing to resolve
        except ValueError:
            pass
        ips = _doh_lookup(host)
        if not ips:
            raise
        out = []
        for ip in ips:
            try:
                out.extend(_orig_getaddrinfo(ip, port, family, type, proto, flags))
            except _socket.gaierror:
                continue
        if not out:
            raise
        return _v4_first(out)

_socket.getaddrinfo = _getaddrinfo_with_fallback

def _http_client(**kw):
    """Plain httpx.Client. (Do NOT use local_address="0.0.0.0": it binds every socket to IPv4,
    so any IPv6 candidate fails with a misleading 'getaddrinfo failed' that hides the real error.)
    IPv4 is preferred instead by ordering DNS results, see _getaddrinfo_with_fallback."""
    return httpx.Client(**kw)

# Browser-style request headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|bmp|ico|avif|heic)(\?|#|/|$)", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|webm|ogg|ogv|mov|m4v|mkv|m3u8|mpd)(\?|#|/|$)", re.I)

_PLACEHOLDER_HINTS = re.compile(
    r"(placeholder|lazy|loading|blank|transparent|spacer|1x1|no[-_]?image|preload|default[-_]?thumb)",
    re.I,
)

SHARED_IMAGE_RE = re.compile(
    r"(?:^|[/_\-])(logo|sprite|banner|share|sharing|"
    r"og[_-]?default|og[_-]?image|twitter[_-]?card|"
    r"default|noimage|no[_-]?image|dummy|"
    r"avatar|flag|icon|favicon|watermark|brand)(?:[/_\-.]|$)",
    re.I,
)

JUNK_TITLES = re.compile(
    r"^(link|links|watch|watch now|play|play now|view|view now|"
    r"download|download now|read more|more|click here|click|"
    r"open|open now|see more|show more|go|next|prev|previous|"
    r"video|photo|image|here|→|»|›|▶|►|•|\-|--)$",
    re.I,
)

VIDEO_HINT_ATTRS = (
    "data-poster", "data-video-poster", "data-video-thumb", "data-video-preview",
    "data-preview", "data-preview-src", "data-video", "data-video-src",
    "data-video-id", "data-video-url", "data-src-video", "poster",
)

PHOTO_PATH_FRAGMENTS = (
    "/photo/", "/photos/", "/image/", "/images/", "/img/",
    "/album/", "/albums/", "/gallery/", "/galleries/",
    "/pic/", "/pics/", "/picture/", "/pictures/",
    "/image-gallery/", "/photo-gallery/",
)

VIDEO_PATH_FRAGMENTS = (
    "/video/", "/videos/", "/watch/", "/play/", "/player/",
    "/embed/", "/v/", "/clip/", "/clips/", "/movie/", "/movies/",
    "/stream/", "/tube/", "/p/",
)

_RESOLVE_CACHE = {}
_CACHE_TTL = 600

_DEFAULT_IMAGE_CACHE = {}

# ----------------------------------------------------------------
# helpers (unchanged)
# ----------------------------------------------------------------
def _absolute(base, url):
    if not url:
        return None
    url = url.strip().strip("'\"")
    if not url:
        return None
    return urljoin(base, url)

def _is_image_url(u):
    if not u:
        return False
    return bool(IMAGE_EXT.search(u.split("?")[0]))

def _is_video_url(u):
    if not u:
        return False
    low = u.lower().split("?")[0]
    if IMAGE_EXT.search(low):
        return False
    return bool(VIDEO_EXT.search(low))

def _looks_like_placeholder(u):
    return bool(u) and bool(_PLACEHOLDER_HINTS.search(u))

def _looks_shared(u):
    return bool(u) and bool(SHARED_IMAGE_RE.search(u))

def _is_acceptable_thumb(u, host=None):
    if not u:
        return False
    if _is_video_url(u):
        return False
    if _looks_like_placeholder(u):
        return False
    if _looks_shared(u):
        return False
    if host:
        defaults = _DEFAULT_IMAGE_CACHE.get(host)
        if defaults and u.split("?")[0] in defaults:
            return False
    return True

def _pick_from_srcset(srcset, base):
    if not srcset:
        return None
    best, best_w = None, -1
    for part in srcset.split(","):
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
    return _absolute(base, best)

def _find_thumbnail(container, base, host=None):
    for v in container.find_all("video"):
        for attr in ("poster", "data-poster", "data-video-poster"):
            u = _absolute(base, v.get(attr))
            if _is_acceptable_thumb(u, host):
                return u
    for img in container.find_all("img"):
        for ss_attr in ("srcset", "data-srcset"):
            u = _pick_from_srcset(img.get(ss_attr), base)
            if _is_acceptable_thumb(u, host):
                return u
        for attr in ("data-src", "data-original", "data-lazy", "data-lazy-src",
                     "data-thumb", "data-thumbnail", "data-image", "data-cover",
                     "data-poster", "data-preview"):
            u = _absolute(base, img.get(attr))
            if _is_acceptable_thumb(u, host):
                return u
        u = _absolute(base, img.get("src"))
        if _is_acceptable_thumb(u, host) and not _looks_like_placeholder(u):
            return u
    for s in container.find_all("source"):
        u = _pick_from_srcset(s.get("srcset") or s.get("data-srcset"), base)
        if _is_acceptable_thumb(u, host):
            return u
        u = _absolute(base, s.get("src") or s.get("data-src"))
        if _is_acceptable_thumb(u, host):
            return u
    for el in [container, *container.find_all(style=True)]:
        style = el.get("style") or ""
        m = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", style)
        if m:
            u = _absolute(base, m.group(1))
            if _is_acceptable_thumb(u, host):
                return u
    for el in [container, *container.find_all(True)]:
        for attr in ("data-bg", "data-background", "data-bg-src", "data-cover"):
            u = _absolute(base, el.get(attr))
            if _is_acceptable_thumb(u, host):
                return u
    return None

def _find_video_source(container, base):
    for v in container.find_all("video"):
        src = v.get("src") or v.get("data-src")
        if _is_video_url(src):
            return _absolute(base, src)
        for s in v.find_all("source"):
            ss = s.get("src") or s.get("data-src")
            if _is_video_url(ss):
                return _absolute(base, ss)
    return None

def _clean_title(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if JUNK_TITLES.match(text):
        return None
    if len(text) <= 2:
        return None
    return text

def _find_title(container, base):
    for attr in ("title", "aria-label", "data-title",
                 "data-name", "data-video-title", "data-original-title",
                 "data-tooltip", "data-label"):
        t = _clean_title(container.get(attr))
        if t:
            return t
    for tag in ("h1", "h2", "h3", "h4", "h5"):
        h = container.find(tag)
        if h:
            t = _clean_title(h.get_text(strip=True))
            if t:
                return t
    img = container.find("img")
    if img and img.get("alt"):
        alt = img["alt"].strip()
        if alt and not _looks_like_placeholder(alt):
            t = _clean_title(alt)
            if t:
                return t
    for cls in ("title", "name", "video-title", "card-title",
                "entry-title", "post-title", "item-title"):
        el = container.find(class_=lambda c: c and cls in c)
        if el:
            t = _clean_title(el.get_text(strip=True))
            if t:
                return t
    for el in container.find_all(["span", "div", "p", "strong", "b"]):
        t = _clean_title(el.get_text(strip=True))
        if t and len(t) > 3 and len(t) < 300 and not JUNK_TITLES.match(t):
            return t
    return None

def _title_from_url(u):
    if not u:
        return None
    slug = urlparse(u).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    slug = re.sub(r"\s+", " ", slug)
    return slug.title() if slug else None

def _card_has_video_hint(container):
    if container.find("video") is not None:
        return True
    for el in [container, *container.find_all(True)]:
        for attr in VIDEO_HINT_ATTRS:
            if el.get(attr):
                return True
    return False

def _url_looks_like_photo(path):
    low = path.lower()
    return any(frag in low for frag in PHOTO_PATH_FRAGMENTS)

def _url_looks_like_video(path):
    low = path.lower()
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
    for skip in ("/login", "/signup", "/register", "/terms", "/privacy",
                 "/contact", "/about", "/faq", "/dmca"):
        if path.lower().startswith(skip):
            return False
    cls_id = " ".join(list(a.get("class") or []) + [a.get("id") or ""]).lower()
    if any(k in cls_id for k in ("navbar", "nav-", "-nav", "menu", "header",
                                 "footer", "breadcrumb", "pagination")):
        return False
    if _card_has_video_hint(a):
        return True
    if _url_looks_like_video(path):
        return True
    return False

def _walk_jsonld(data):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _walk_jsonld(v)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_jsonld(item)

def _current_page_number(url):
    path = urlparse(url).path
    qs = dict(parse_qsl(urlparse(url).query))
    for key in ("page", "p", "paged", "pg"):
        if key in qs:
            try:
                return int(qs[key])
            except Exception:
                pass
    m = re.search(r"/page/(\d+)", path)
    if m:
        return int(m.group(1))
    m = re.search(r"/(\d+)/?$", path)
    if m:
        return int(m.group(1))
    return 1

def _guess_next_page(base, page_num):
    nxt = page_num + 1
    parts = urlsplit(base)
    path = parts.path
    qs = dict(parse_qsl(parts.query))
    for key in ("page", "p", "paged", "pg"):
        if key in qs:
            qs[key] = str(nxt)
            return urlunsplit((parts.scheme, parts.netloc, path,
                               urlencode(qs), parts.fragment))
    m = re.search(r"/page/(\d+)/?", path)
    if m:
        new_path = re.sub(r"/page/\d+/?", f"/page/{nxt}/", path)
        return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, ""))
    if re.search(r"/\d+/?$", path):
        new_path = re.sub(r"/\d+/?$", f"/{nxt}/", path)
        return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, ""))
    qs["page"] = str(nxt)
    return urlunsplit((parts.scheme, parts.netloc, path,
                       urlencode(qs), parts.fragment))

def _find_next_page_link(soup, base, base_host):
    tag = soup.find("link", rel="next") or soup.find("a", rel="next")
    if tag and tag.get("href"):
        return urljoin(base, tag["href"])
    current_page_num = _current_page_number(base)
    numeric = []
    for a in soup.find_all("a", href=True):
        text = (a.get_text(strip=True) or "").strip()
        if not re.fullmatch(r"\d+", text):
            continue
        try:
            n = int(text)
        except Exception:
            continue
        if n <= current_page_num:
            continue
        full = urljoin(base, a["href"])
        if base_host and base_host not in urlparse(full).netloc:
            continue
        numeric.append((n, full))
    if numeric:
        numeric.sort(key=lambda x: x[0])
        return numeric[0][1]
    candidates = []
    for a in soup.find_all("a", href=True):
        text = (a.get_text(strip=True) or "").lower()
        aria = (a.get("aria-label") or "").lower()
        rel = (a.get("rel") or [])
        cls = " ".join(a.get("class") or []).lower()
        title = (a.get("title") or "").lower()
        blob = f"{text} {aria} {' '.join(rel)} {cls} {title}"
        if any(k in blob for k in ("next", "older", "→", "»", "›")):
            href = a["href"]
            if href.startswith(("#", "javascript:", "mailto:")):
                continue
            full = urljoin(base, href)
            if base_host and base_host not in urlparse(full).netloc:
                continue
            candidates.append(full)
    return candidates[0] if candidates else None

def _record_default_images(host, soup):
    if not host:
        return
    counts = Counter()
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy",
                     "data-thumb", "data-poster"):
            v = img.get(attr)
            if v and not _looks_like_placeholder(v):
                counts[v.split("?")[0]] += 1
    for v in soup.find_all("video"):
        p = v.get("poster")
        if p and not _looks_like_placeholder(p):
            counts[p.split("?")[0]] += 1
    defaults = _DEFAULT_IMAGE_CACHE.setdefault(host, set())
    for url, n in counts.items():
        if n >= 3 or _looks_shared(url):
            defaults.add(url)
    for prop in ("og:image:secure_url", "og:image:url", "og:image",
                 "twitter:image", "twitter:image:src"):
        tag = (soup.find("meta", property=prop)
               or soup.find("meta", attrs={"name": prop}))
        if tag and tag.get("content"):
            u = tag["content"]
            if not u.startswith("http"):
                u = urljoin("http://" + host, u)
            defaults.add(u.split("?")[0])

# ----------------------------------------------------------------
# video cards scraper (unchanged)
# ----------------------------------------------------------------
def _extract_items_from_soup(soup, base, max_items=80):
    base_host = urlparse(base).netloc
    _record_default_images(base_host, soup)
    raw_items = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        if not _is_video_preview(a, base_host):
            continue
        full = urljoin(base, a["href"]).split("#")[0]
        if full.rstrip("/") == base.rstrip("/"):
            continue
        key = full.split("?")[0]
        if key in seen_links:
            continue
        seen_links.add(key)
        thumb = _find_thumbnail(a, base, host=base_host)
        raw_items.append({
            "title": _find_title(a, base) or _title_from_url(full) or full,
            "thumbnail": thumb,
            "video_src": _find_video_source(a, base),
            "link": full,
            "page": base,
            "uid": hashlib.md5(full.encode("utf-8")).hexdigest()[:12],
        })
        if len(raw_items) >= max_items:
            break
    thumb_counts = Counter(it["thumbnail"] for it in raw_items if it.get("thumbnail"))
    for it in raw_items:
        th = it.get("thumbnail")
        if th and thumb_counts[th] >= 3:
            it["thumbnail"] = None
    return raw_items

def scrape_page(url: str, max_items: int = 80, page_num: None = None):
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=20) as client:
            r = client.get(url)
            r.raise_for_status()
            html = r.text
            base = str(r.url)
    except Exception as e:
        return {"page": url, "items": [], "count": 0, "error": str(e),
                "next_page": None, "page_num": page_num}
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base).netloc
    page_title = soup.title.string.strip() if soup.title and soup.title.string else base
    items = _extract_items_from_soup(soup, base, max_items=max_items)
    next_page = _find_next_page_link(soup, base, base_host)
    explicit_next = bool(next_page)
    if not next_page:
        pn = page_num if page_num is not None else _current_page_number(base)
        if items:
            next_page = _guess_next_page(base, pn)
    return {
        "page": base,
        "page_title": page_title,
        "items": items,
        "count": len(items),
        "next_page": next_page,
        "next_is_guess": (not explicit_next) and bool(next_page),
        "page_num": page_num if page_num is not None else _current_page_number(base),
    }

# ----------------------------------------------------------------
# CATEGORIES - only real cards with images (FIXED)
# ----------------------------------------------------------------
CATEGORY_SKIP_PATH_PREFIXES = (
    "/login", "/signup", "/register", "/terms", "/privacy",
    "/contact", "/about", "/faq", "/dmca", "/page/",
)

CATEGORY_SKIP_CLASS_HINTS = (
    "navbar", "nav-", "-nav", "menu", "header", "footer",
    "breadcrumb", "pagination",
)

_NUMERIC_ONLY = re.compile(r"^\d+$")

def _is_category_link(a, base_host):
    href = a.get("href", "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    full = urljoin("http://" + base_host, href) if not href.startswith("http") else href
    parsed = urlparse(full)
    if base_host and base_host not in parsed.netloc:
        return False
    path = (parsed.path or "/").lower()
    if any(path.startswith(skip) for skip in CATEGORY_SKIP_PATH_PREFIXES):
        return False
    has_image = bool(a.find("img")) or bool(a.find("video"))
    if not has_image:
        return False
    cls_id = " ".join(list(a.get("class") or []) + [a.get("id") or ""]).lower()
    if any(k in cls_id for k in CATEGORY_SKIP_CLASS_HINTS):
        return False
    if re.search(r"/category/|/cat/|/tags/|\/c\/|\/video-category/", path) or "/category" in path or "/cat/" in path or "/tags/" in path:
        return True
    return True

def _clean_category_name(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[\s\n]*[\d,]+\s*(?:videos?|vids?|clips?)?\s*$", "", text, flags=re.I).strip()
    if not text or len(text) < 2 or len(text) > 80:
        return None
    if _NUMERIC_ONLY.match(text):
        return None
    if JUNK_TITLES.match(text):
        return None
    if re.search(r"\blogo\b", text, re.I):
        return None
    return text

# ---- Strip page chrome (header / nav / language switcher / footer) ----
_CHROME_TAGS = ("header", "nav", "footer", "aside", "script", "style", "noscript")
_CHROME_ATTR_RE = re.compile(
    r"(^|[\s_-])(lang|language|languages|locale|flags?|navbar|nav|menu|topbar|top-bar|"
    r"header|footer|breadcrumbs?|sidebar|dropdown|pagination)([\s_-]|$)", re.I)
_LANG_HREF_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?)/?$", re.I)
_MAIN_SELECTORS = (
    "main", "[role=main]", "#content", "#main", ".content", ".main-content",
    ".list-categories", ".categories", ".categories-list", ".category-list",
    ".container",
)

def _is_chrome(el):
    if el.name in _CHROME_TAGS:
        return True
    if el.get("role") in ("navigation", "banner", "contentinfo"):
        return True
    if el.name == "a":
        href = (el.get("href") or "").strip()
        if el.get("hreflang") or _LANG_HREF_RE.match(href):
            return True
    attrs = " ".join(list(el.get("class") or []) + [el.get("id") or ""])
    return bool(attrs.strip() and _CHROME_ATTR_RE.search(attrs))

def _body_only(soup):
    """Return a copy of the soup reduced to the main body content."""
    import copy
    work = copy.copy(soup)
    for el in list(work.find_all(True)):
        if el.parent is None:
            continue
        try:
            if _is_chrome(el):
                el.decompose()
        except Exception:
            pass
    for sel in _MAIN_SELECTORS:
        node = work.select_one(sel)
        if node and node.find("a", href=True) and node.find("img"):
            return node
    return work.body or work

def _extract_categories_from_soup(soup, base, max_items=200):
    base_host = urlparse(base).netloc
    _record_default_images(base_host, soup)
    best_by_key = {}
    order = []
    for a in soup.find_all("a", href=True):
        if not _is_category_link(a, base_host):
            continue
        full = urljoin(base, a["href"]).split("#")[0]
        if full.rstrip("/") == base.rstrip("/"):
            continue
        path = (urlparse(full).path or "/").rstrip("/").lower()
        if path in ("/categories", "/category", "/cats", "/sites"):
            continue
        if re.match(r"^/[a-z]{2}(/|$)", path) and (path.endswith("/sites") or path.endswith("/categories") or path.endswith("/cats")):
            continue
        key = full.split("?")[0]
        name = (_clean_category_name(_find_title(a, base))
                or _clean_category_name(_title_from_url(full)))
        if not name:
            continue
        thumb = _find_thumbnail(a, base, host=base_host)
        candidate = {
            "name": name,
            "thumbnail": thumb,
            "link": full,
            "uid": hashlib.md5(full.encode("utf-8")).hexdigest()[:12],
            "_has_thumb": bool(thumb),
        }
        prev = best_by_key.get(key)
        if prev is None:
            best_by_key[key] = candidate
            order.append(key)
        else:
            if candidate["_has_thumb"] and not prev["_has_thumb"]:
                best_by_key[key] = candidate
            elif candidate["_has_thumb"] == prev["_has_thumb"] and not prev.get("name"):
                best_by_key[key] = candidate
        if len(order) >= max_items * 3:
            break
    with_thumb = []
    without_thumb = []
    for key in order:
        it = best_by_key[key]
        has = it.pop("_has_thumb", False)
        if has and it.get("thumbnail"):
            with_thumb.append(it)
        else:
            without_thumb.append(it)
    raw = with_thumb if with_thumb else without_thumb
    raw = raw[:max_items]
    thumb_counts = Counter(it["thumbnail"] for it in raw if it.get("thumbnail"))
    for it in raw:
        th = it.get("thumbnail")
        if th and thumb_counts[th] >= 6:
            it["thumbnail"] = None
    return raw

def scrape_categories(url: str, max_items: int = 200, page_num: None = None):
    if _is_fp_tags_url(url):
        return scrape_tags(url)
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=20) as client:
            r = client.get(url)
            r.raise_for_status()
            html = r.text
            base = str(r.url)
    except Exception as e:
        return {
            "page": url,
            "categories": [],
            "count": 0,
            "error": str(e),
            "next_page": None,
            "page_num": page_num,
        }
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base).netloc
    page_title = soup.title.string.strip() if soup.title and soup.title.string else base
    categories = _extract_categories_from_soup(soup, base, max_items=max_items)
    next_page = _find_next_page_link(soup, base, base_host)
    explicit_next = bool(next_page)
    if not next_page:
        pn = page_num if page_num is not None else _current_page_number(base)
        if categories:
            next_page = _guess_next_page(base, pn)
    return {
        "page": base,
        "page_title": page_title,
        "categories": categories,
        "count": len(categories),
        "next_page": next_page,
        "next_is_guess": (not explicit_next) and bool(next_page),
        "page_num": page_num if page_num is not None else _current_page_number(base),
    }

# ----------------------------------------------------------------
# TAGS - plain text tag pills grouped by letter (no thumbnails)
# ----------------------------------------------------------------
def _clean_tag_name(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    # drop trailing counts like "(123)" or "123 videos" but keep names like "69"
    text = re.sub(r"\s*\(\s*[\d,\.]+\s*\)\s*$", "", text)
    text = re.sub(r"\s+[\d,\.]+\s*(?:videos?|vids?|clips?)\s*$", "", text, flags=re.I)
    text = text.strip()
    if not text or len(text) > 60:
        return None
    return text

def _tag_letter(name):
    ch = (name or "?")[0].upper()
    return ch if ch.isalpha() else "#"

def _collect_tag_links(root, base, base_host):
    found = []
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base, href).split("#")[0]
        p = urlparse(full)
        if base_host and p.netloc != base_host:
            continue
        segs = [x for x in (p.path or "/").split("/") if x]
        if len(segs) < 2:            # /categories/ , /sites/ , /ru/ ... are not tags
            continue
        name = _clean_tag_name(a.get_text(" ", strip=True) or a.get("title"))
        if not name:
            continue
        found.append((segs[0].lower(), full, name))
    return found

_SITE_SECTIONS = ("sites", "site", "networks", "network", "studios", "studio", "channels", "channel")

def _extract_az_tags(soup, base, max_items=2000, sections=("categories",)):
    """Exact extraction of the A-Z block (letter rows -> text links). Returns [] if not found."""
    rows = soup.select("#custom_list_categories_categories_list_items .list-categories__row")
    if not rows:
        rows = soup.select(".list-categories__row")
    tags, seen = [], set()
    for row in rows:
        letter_el = row.select_one(".list-categories__row--letter")
        letter = (letter_el.get_text(strip=True) if letter_el else "") or None
        for a in row.select(".list-categories__row--list a[href]"):
            name = _clean_tag_name(a.get_text(" ", strip=True) or a.get("title"))
            if not name:
                continue
            full = urljoin(base, a["href"].strip()).split("#")[0]
            _segs = [x.lower() for x in urlparse(full).path.split("/") if x]
            if len(_segs) < 2 or _segs[0] not in sections and not any(x in sections for x in _segs[:-1]):
                continue
            key = full.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            tags.append({
                "name": name,
                "link": full,
                "thumbnail": None,
                "letter": (letter or _tag_letter(name)).upper()[:1] if (letter or name[:1].isalpha()) else "#",
                "uid": hashlib.md5(full.encode("utf-8")).hexdigest()[:12],
            })
            if len(tags) >= max_items:
                return tags
    return tags

def _extract_tags_from_soup(soup, base, max_items=2000):
    exact = _extract_az_tags(soup, base, max_items=max_items)
    if exact:
        return exact
    base_host = urlparse(base).netloc
    found = _collect_tag_links(_body_only(soup), base, base_host)
    if not found:
        found = _collect_tag_links(soup, base, base_host)
    if not found:
        return []
    # keep only the dominant URL section (e.g. "categories") so stray links drop out
    top = Counter(seg for seg, _, _ in found).most_common(1)[0][0]
    tags, seen = [], set()
    for seg, full, name in found:
        key = full.split("?")[0].rstrip("/")
        if seg != top or key in seen:
            continue
        seen.add(key)
        tags.append({
            "name": name,
            "link": full,
            "thumbnail": None,
            "letter": _tag_letter(name),
            "uid": hashlib.md5(full.encode("utf-8")).hexdigest()[:12],
        })
        if len(tags) >= max_items:
            break
    return tags

_TAGS_CACHE = {}
_TAGS_TTL = 600

def _is_fp_tags_url(url: str) -> bool:
    """fullporno.to /categories/ (optionally with a language prefix) = A-Z text tag list."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.netloc or "").lower()
    if not host.endswith("fullporno.to"):
        return False
    return re.fullmatch(r"(/[a-z]{2})?/categories", (p.path or "/").rstrip("/").lower()) is not None

def _extract_site_names(soup, base, max_items=2000):
    """Text-only site/network names. Prefers the A-Z block; otherwise reads plain links that point
    into /sites/<x>/ or /networks/<x>/ (names only - never thumbnails, never the videos shown
    next to them)."""
    exact = _extract_az_tags(soup, base, max_items=max_items, sections=_SITE_SECTIONS)
    if exact:
        return exact
    base_host = urlparse(base).netloc
    out, seen = [], set()
    for root in (_body_only(soup), soup):
        for seg, full, name in _collect_tag_links(root, base, base_host):
            if seg not in _SITE_SECTIONS:
                continue
            key = full.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "name": name, "link": full, "thumbnail": None,
                "letter": _tag_letter(name),
                "uid": hashlib.md5(full.encode("utf-8")).hexdigest()[:12],
            })
            if len(out) >= max_items:
                break
        if out:
            break
    out.sort(key=lambda t: t["name"].casefold())
    return out

def scrape_tags(url: str, max_items: int = 2000, kind: str = "porntags", max_pages: int = 4):
    """Text-only A-Z list (kind='porntags' -> tags/categories, kind='pornsites' -> sites/networks).
    Reads ONLY the names from the listing page(s); the videos behind a name are scraped later,
    when the user opens it."""
    ckey = (url, kind)
    hit = _TAGS_CACHE.get(ckey)
    if hit and time.time() - hit[0] < _TAGS_TTL:
        return hit[1]
    is_sites = (kind == "pornsites")
    tags, seen_keys, seen_pages = [], set(), set()
    title, base, html = None, url, ""
    next_url, pages_read = url, 0
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            while next_url and pages_read < (max_pages if is_sites else 1) and next_url not in seen_pages:
                seen_pages.add(next_url)
                r = client.get(next_url)
                r.raise_for_status()
                html, base = r.text, str(r.url)
                soup = BeautifulSoup(html, "lxml")
                if title is None:
                    title = soup.title.string.strip() if soup.title and soup.title.string else base
                found = (_extract_site_names(soup, base, max_items=max_items) if is_sites
                         else _extract_az_tags(soup, base, max_items=max_items))
                for t in found:
                    k = t["link"].split("?")[0].rstrip("/")
                    if k not in seen_keys:
                        seen_keys.add(k)
                        tags.append(t)
                pages_read += 1
                # only follow an EXPLICIT "next" link, and only for the sites list
                next_url = _find_next_page_link(soup, base, urlparse(base).netloc) if (is_sites and found) else None
    except Exception as e:
        if not tags:
            return {"page": url, "categories": [], "count": 0, "error": str(e),
                    "next_page": None, "page_num": 1}
    if not tags:
        what = "site list" if is_sites else "A-Z tag list"
        return {"page": base, "page_title": title, "categories": [], "count": 0,
                "error": f"{what.capitalize()} not found in the page the server received "
                         f"({len(html)} bytes) - the site may be blocking the scraper.",
                "next_page": None, "page_num": 1}
    out = {"page": base, "page_title": title, "categories": tags[:max_items], "count": len(tags[:max_items]),
           "next_page": None, "next_is_guess": False, "page_num": 1, "kind": kind}
    _TAGS_CACHE[ckey] = (time.time(), out)
    return out

# ----------------------------------------------------------------
# STUDIO SECTIONS - the "sites" listing page groups its own preview
# videos under each studio name ("WIFEY", "See All 87 Videos", a few
# video cards). We read exactly that off ONE page fetch, no per-site scrape.
# ----------------------------------------------------------------
def _clean_video_count(text):
    m = re.search(r"([\d,]+)", text or "")
    return int(m.group(1).replace(",", "")) if m else None

def _extract_studio_video(item, base):
    a = item.find("a", href=True)
    if not a:
        return None
    link = urljoin(base, a["href"].split("#")[0])
    title = (a.get("title") or "").strip()
    if not title:
        strong = item.select_one(".item-info .title")
        title = strong.get_text(strip=True) if strong else _title_from_url(link)
    img = item.select_one("img")
    thumb = None
    if img:
        for attr in ("data-src", "data-savepage-src", "src"):
            v = img.get(attr)
            if v and v.startswith("http"):
                thumb = v
                break
    dur_el = item.select_one(".duration")
    duration = dur_el.get_text(strip=True) if dur_el else None
    return {"title": title, "link": link, "thumbnail": thumb, "duration": duration}

def _extract_studio_sections(soup, base, max_sections=100):
    container = soup.select_one("#list_content_sources_sponsors_list_items") or soup
    kids = container.find_all("div", recursive=False)
    sections = []
    i = 0
    while i < len(kids) and len(sections) < max_sections:
        el = kids[i]
        if "headline" in (el.get("class") or []):
            h = el.find(["h1", "h2"])
            name = h.get_text(strip=True) if h else None
            more = el.find("a", class_="more")
            link = urljoin(base, more["href"]) if more and more.get("href") else None
            count_span = more.find("span") if more else None
            video_count = _clean_video_count(
                count_span.get_text() if count_span else (more.get_text() if more else "")
            )
            videos = []
            if i + 1 < len(kids) and "box" in (kids[i + 1].get("class") or []):
                for item in kids[i + 1].select(".list-videos .item"):
                    v = _extract_studio_video(item, base)
                    if v:
                        videos.append(v)
                i += 1
            if name and link:
                sections.append({"name": name, "link": link, "video_count": video_count, "videos": videos})
        i += 1
    return sections

_STUDIOS_CACHE = {}
_STUDIOS_TTL = 600

def scrape_studio_sections(url: str, max_sections: int = 60):
    """One page fetch: every studio section (name, "See All" link, and the few
    preview videos already shown for it) exactly as the sites page lists them."""
    hit = _STUDIOS_CACHE.get(url)
    if hit and time.time() - hit[0] < _STUDIOS_TTL:
        return hit[1]
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            r = client.get(url)
            r.raise_for_status()
            html, base = r.text, str(r.url)
    except Exception as e:
        return {"page": url, "sections": [], "count": 0, "error": str(e),
                "next_page": None, "page_num": 1}
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else base
    sections = _extract_studio_sections(soup, base, max_sections=max_sections)
    next_page = _find_next_page_link(soup, base, urlparse(base).netloc)
    explicit_next = bool(next_page)
    page_num = _current_page_number(base)
    if not next_page and sections:
        next_page = _guess_next_page(base, page_num)
    if not sections:
        return {"page": base, "page_title": title, "sections": [], "count": 0,
                "error": f"Studio sections not found in the page the server received "
                         f"({len(html)} bytes) - the site may be blocking the scraper.",
                "next_page": None, "page_num": page_num}
    out = {
        "page": base,
        "page_title": title,
        "sections": sections,
        "count": len(sections),
        "next_page": next_page,
        "next_is_guess": (not explicit_next) and bool(next_page),
        "page_num": page_num,
        "kind": "pornsites",
    }
    _STUDIOS_CACHE[url] = (time.time(), out)
    return out

def scrape_many(urls, download: bool = False, verify: bool = True):
    results_map = {}
    def work(u):
        try:
            if isinstance(u, dict):
                url, pn = u.get("url"), u.get("page_num")
            else:
                url, pn = u, None
            res = scrape_page(url, page_num=pn)
            return url, res
        except Exception as e:
            return u, {"page": u, "items": [], "count": 0, "error": str(e),
                       "next_page": None, "page_num": None}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f in as_completed([pool.submit(work, u) for u in urls]):
            u, res = f.result()
            key = u if isinstance(u, str) else (u.get("url") if isinstance(u, dict) else str(u))
            results_map[key] = res
    out = []
    for u in urls:
        key = u if isinstance(u, str) else (u.get("url") if isinstance(u, dict) else str(u))
        out.append(results_map[key])
    return out

# ----------------------------------------------------------------
# resolve functions (unchanged)
# ----------------------------------------------------------------
def _classify(u):
    low = u.lower().split("?")[0].rstrip("/")
    if low.endswith((".mp4", ".m4v", ".mov")):
        return "mp4"
    if low.endswith(".webm"):
        return "webm"
    if low.endswith(".m3u8") or ".m3u8" in u.lower():
        return "hls"
    if low.endswith(".mpd"):
        return "dash"
    if "/get_file/" in low or "/get_stream/" in low:
        return "mp4"
    return "other"

def _collect_from_html(html, base):
    found = []
    patterns = [
        r'<video[^>]+src=["\']([^"\']+?\.(?:mp4|m3u8|webm|mov|mpd)[^"\']*)["\']',
        r'<source[^>]+src=["\']([^"\']+?\.(?:mp4|m3u8|webm|mov|mpd)[^"\']*)["\']',
        r'<meta[^>]+(?:property|name)=["\'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["\'][^>]+content=["\']([^"\']+)["\']',
        r'"contentUrl"\s*:\s*"([^"]+\.(?:mp4|m3u8|webm|mov|mpd)[^"]*)"',
        r'"embedUrl"\s*:\s*"([^"]+\.(?:mp4|m3u8|webm|mov|mpd)[^"]*)"',
        r'(?:file|video_url|videoUrl|videoSrc|hls_url|mp4_url)\s*[:=]\s*["\']([^"\']+?\.(?:mp4|m3u8|webm|mov|mpd)[^"\']*)["\']',
        r'sources\s*:\s*\[\s*\{[^}]*?"file"\s*:\s*"([^"]+\.(?:mp4|m3u8|webm|mov|mpd)[^"]*)"',
        r'["\'](?:hls|mp4|source)["\']\s*:\s*["\']([^"\']+?\.(?:mp4|m3u8|webm|mov)[^"\']*)["\']',
        r'["\'](https?://[^"\']+?\.(?:mp4|m3u8|webm|mov|mpd)(?:/|\?[^"\']*)?)["\']',
        r'["\'](//[^"\']+?\.(?:mp4|m3u8|webm|mov|mpd)(?:/|\?[^"\']*)?)["\']',
        r'(https?://[^"\'\s<>]+/get_file/[^"\'\s<>]+?\.(?:mp4|webm|mov)(?:/|\?[^"\'\s<>]*)?)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.I | re.S):
            raw = m.group(1)
            u = raw.replace("\\/", "/").strip()
            if u.startswith("//"):
                u = "https:" + u
            elif not u.startswith("http"):
                u = urljoin(base, u)
            if _is_image_url(u):
                continue
            if not _is_video_url(u):
                continue
            found.append(u)
    return found

def _extract_detail_thumbnail(soup, base, host=None):
    def _ok(u):
        return _is_acceptable_thumb(u, host)
    for prop in ("og:image:secure_url", "og:image:url", "og:image",
                 "twitter:image", "twitter:image:src"):
        tag = (soup.find("meta", property=prop)
               or soup.find("meta", attrs={"name": prop}))
        if tag and tag.get("content"):
            u = _absolute(base, tag["content"])
            if _ok(u):
                return u
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for entry in _walk_jsonld(data):
                if isinstance(entry, dict):
                    for k in ("thumbnailUrl", "image", "thumbnail"):
                        v = entry.get(k)
                        if isinstance(v, list) and v:
                            v = v[0]
                        if isinstance(v, dict):
                            v = v.get("url") or v.get("contentUrl")
                        u = _absolute(base, v)
                        if _ok(u):
                            return u
        except Exception:
            pass
    for v in soup.find_all("video"):
        u = _absolute(base, v.get("poster") or v.get("data-poster"))
        if _ok(u):
            return u
    best, best_score = None, 0
    for img in soup.find_all("img"):
        u = None
        for attr in ("data-src", "data-original", "data-lazy",
                     "data-thumb", "data-poster", "data-cover", "src"):
            cand = _absolute(base, img.get(attr))
            if _ok(cand):
                u = cand
                break
        if not u:
            continue
        score = 1
        try:
            w = int(str(img.get("width", "0")).replace("px", "") or 0)
            h = int(str(img.get("height", "0")).replace("px", "") or 0)
            score = max(w, h)
        except Exception:
            pass
        if any(k in u.lower() for k in ("thumb", "poster", "preview", "cover")):
            score += 1000
        if score > best_score:
            best, best_score = u, score
    return best

def _is_preview_media_url(url: str):
    low = (url or "").lower()
    if "/preview/" in low or "cast." in low and "preview" in low:
        return True
    return bool(re.search(
        r"(?:preview|previews|thumbnail|thumb|poster|teaser|sample|trailer|sprite|storyboard|hover|lowres|low-res|low_quality|small)(?:[._\-/]|$)",
        low,
    ))

def _score_video_candidate(item):
    src = item.get("src", "")
    low = src.lower()
    score = 0
    if _is_preview_media_url(src):
        score -= 1000
    if item.get("type") == "mp4":
        score += 30
    elif item.get("type") == "hls":
        score += 25
    elif item.get("type") == "dash":
        score += 20
    elif item.get("type") == "webm":
        score += 15
    if "/get_file/" in low:
        score += 80
    if re.search(r"_(2160|1440|1080|720|480|360)p?(?:m)?\.mp4", low):
        score += 40
        m = re.search(r"_(2160|1440|1080|720|480|360)", low)
        if m:
            score += int(m.group(1)) // 10
    if any(k in low for k in ("full", "original", "source", "master", "playlist")):
        score += 20
    if any(k in low for k in ("preview", "thumb", "poster", "sample", "teaser", "trailer")):
        score -= 500
    if "dreamserve" in low:
        score -= 50
    return score

def resolve_full_video_url(detail_url: str, light: bool = False, fresh: bool = False):
    result = resolve_video_url(detail_url, light=light, fresh=fresh)
    if not isinstance(result, dict):
        return {"error": "Invalid resolver response", "detail_url": detail_url,
                "video": None, "all": [], "thumbnail": None}
    candidates = result.get("all") or []
    if not candidates:
        return {**result, "video": None, "full_video": None}
    ranked = sorted(candidates, key=_score_video_candidate, reverse=True)
    full_video = ranked[0] if ranked and _score_video_candidate(ranked[0]) > -900 else None
    return {
        **result,
        "video": full_video,
        "full_video": full_video,
    }

_OK_APP_KEY = "CBAFJIICABABABABA"
_OK_SESSION_KEY = "-s-280i1-fBE732Y5lav2k7Z3hcFfpi.2-Yl2272eEcH13hc"
_OK_VID_RE = re.compile(
    r"generate_mp4\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"][^'\"]+['\"]\s*,\s*['\"]?(\d{8,})['\"]?",
    re.I,
)
_OK_URL_TAGS = (
    "url_ultrahd", "url_quadhd", "url_fullhd", "url_high",
    "url_medium", "url_low", "url_mobile", "url_tiny",
)

def _extract_ok_ru_videos(html: str, detail_url: str):
    m = _OK_VID_RE.search(html or "")
    if not m:
        m = re.search(r"generate_mp4\s*\([^)]*?(\d{10,})[^)]*\)", html or "", re.I)
    if not m:
        return []
    vid = m.group(1)
    api = (
        "https://api.ok.ru/fb.do"
        f"?application_key={_OK_APP_KEY}"
        "&fields="
        + quote_plus(
            "video.url_tiny,video.url_low,video.url_high,video.url_medium,"
            "video.url_quadhd,video.url_mobile,video.url_ultrahd,video.url_fullhd"
        )
        + f"&method=video.get&session_key={_OK_SESSION_KEY}&vids={vid}"
    )
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            r = client.get(
                api,
                headers={
                    **HEADERS,
                    "Referer": detail_url or "https://www.bdsmhole.com/",
                    "Origin": "https://www.bdsmhole.com",
                    "Accept": "application/xml,text/xml,*/*",
                },
            )
            r.raise_for_status()
            text = r.text
    except Exception:
        return []
    found = []
    for tag in _OK_URL_TAGS:
        for mm in re.finditer(
            rf"<{tag}>\s*(https?://[^<\s]+)\s*</{tag}>",
            text,
            re.I,
        ):
            u = mm.group(1).replace("&amp;", "&").strip()
            if u and not _is_image_url(u):
                found.append({"src": u, "type": "mp4"})
        if found:
            break
    seen = set()
    out = []
    for item in found:
        key = item["src"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

def resolve_video_url(detail_url: str, light: bool = False, fresh: bool = False):
    """light=True is used by the availability check: tighter timeouts and no player-iframe
    requests when the page already exposes a real (non-preview) media link."""
    now = time.time()
    cached = None if fresh else _RESOLVE_CACHE.get(detail_url)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    ckey = ("L|" + detail_url) if light else detail_url
    if light and not fresh:
        lc = _RESOLVE_CACHE.get(ckey)
        if lc and now - lc[0] < _CACHE_TTL:
            return lc[1]
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=(8 if light else 20)) as client:
            r = client.get(detail_url)
            r.raise_for_status()
            html = r.text
            base = str(r.url)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        # 404 / 410 = the video page was removed from the original site
        return {"error": str(e), "detail_url": detail_url, "status": code,
                "unavailable": code in (404, 410),
                "video": None, "all": [], "thumbnail": None}
    except Exception as e:
        return {"error": str(e), "detail_url": detail_url, "unavailable": False,
                "video": None, "all": [], "thumbnail": None}
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base).netloc
    candidates = _collect_from_html(html, base)
    iframe_failed = False
    skip_iframes = False
    if light:
        direct = [u for u in candidates
                  if not _is_image_url(u) and _classify(u) != "other" and not _is_preview_media_url(u)]
        skip_iframes = bool(direct)
    for iframe in ([] if skip_iframes else soup.find_all("iframe")):
        src = iframe.get("src")
        if not src:
            continue
        player_url = urljoin(base, src)
        try:
            with _http_client(headers=HEADERS, follow_redirects=True, timeout=(6 if light else 15)) as client:
                pr = client.get(player_url, headers={**HEADERS, "Referer": detail_url})
                candidates.extend(_collect_from_html(pr.text, player_url))
        except Exception:
            iframe_failed = True   # transient - do not treat as deleted
    seen = set()
    out = []
    for u in candidates:
        if _is_image_url(u):
            continue
        key = u.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        t = _classify(u)
        if t == "other":
            if "okcdn.ru" in u.lower() or "ok.ru" in u.lower():
                t = "mp4"
            else:
                continue
        out.append({"src": u, "type": t})
    ok_videos = _extract_ok_ru_videos(html, detail_url)
    if ok_videos:
        ok_srcs = {x["src"].split("?")[0] for x in ok_videos}
        out = [x for x in out if x["src"].split("?")[0] not in ok_srcs]
        out = [x for x in out if "/get_stream/" not in (x.get("src") or "").lower()]
        out = ok_videos + out
    result = {
        "detail_url": detail_url,
        "thumbnail": _extract_detail_thumbnail(soup, base, host=host),
        "video": out[0] if out else None,
        "all": out,
        # page loaded fine but no playable media anywhere = deleted / dead video
        "unavailable": (not out) and (not iframe_failed),
    }
    if out or not iframe_failed:
        _RESOLVE_CACHE[ckey] = (now, result)
    return result

# ----------------------------------------------------------------
# search (unchanged)
# ----------------------------------------------------------------
def _build_search_urls(site, query):
    site = site.rstrip("/")
    if not site.startswith("http"):
        site = "https://" + site
    q = quote_plus(query)
    return [
        f"{site}/?s={q}",
        f"{site}/search?q={q}",
        f"{site}/search/{q}",
        f"{site}/search/{q}/",
        f"{site}/?q={q}",
        f"{site}/videos/search?q={q}",
    ]

def _homepage_url(site):
    site = site.rstrip("/")
    if not site.startswith("http"):
        site = "https://" + site
    return site + "/"

def _item_link_set(items):
    return {it.get("link", "").split("?")[0] for it in items if it.get("link")}

def search_one(site, query, max_items=80):
    last = None
    try:
        home = scrape_page(_homepage_url(site), max_items=max_items)
        home_links = _item_link_set(home.get("items", []))
    except Exception:
        home_links = set()
    for url in _build_search_urls(site, query):
        try:
            res = scrape_page(url, max_items=max_items)
        except Exception as e:
            res = {"page": url, "items": [], "count": 0, "error": str(e),
                   "next_page": None}
        res["query"] = query
        res["site"] = site
        res["search_url"] = url
        last = res
        items = res.get("items") or []
        if not items:
            continue
        links = _item_link_set(items)
        if not home_links:
            if query.lower() in url.lower():
                res["source"] = "site-search"
                return res
            continue
        if links == home_links:
            res["source"] = "homepage-fallback"
            res["items"] = []
            last = res
            continue
        overlap = len(links & home_links) / max(len(links), 1)
        if overlap >= 0.9:
            res["source"] = "homepage-fallback"
            res["items"] = []
            last = res
            continue
        res["source"] = "site-search"
        return res
    if last is not None:
        last.setdefault("source", "none")
    return last

def search_many(sites, query, max_items=80):
    results_map = {}
    def work(site):
        try:
            res = search_one(site, query, max_items=max_items)
            return site, res
        except Exception as e:
            return site, {"page": site, "site": site, "query": query,
                          "items": [], "count": 0, "error": str(e),
                          "next_page": None, "source": "none"}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f in as_completed([pool.submit(work, s) for s in sites]):
            s, res = f.result()
            results_map[s] = res
    return [results_map[s] for s in sites]