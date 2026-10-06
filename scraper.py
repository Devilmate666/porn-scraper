"""Extract video preview elements from a site's homepage + resolve detail page to real stream."""

import atexit
import copy
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

# Keep a single true original getaddrinfo even across module reloads
if not getattr(_socket, "_grok_dns_orig", None):
    _socket._grok_dns_orig = _socket.getaddrinfo
_orig_getaddrinfo = _socket._grok_dns_orig
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

if not getattr(_socket, "_grok_dns_patched", False):
    _socket.getaddrinfo = _getaddrinfo_with_fallback
    _socket._grok_dns_patched = True

# Browser-style request headers (simple — avoid multi-retry hangs)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _http_client(**kw):
    """Plain httpx.Client. IPv4 preferred via DNS result ordering."""
    kw.setdefault("headers", HEADERS)
    kw.setdefault("follow_redirects", True)
    return httpx.Client(**kw)


_FETCH_CLIENT = httpx.Client(
    headers=HEADERS,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=40, max_keepalive_connections=20, keepalive_expiry=30),
)
atexit.register(_FETCH_CLIENT.close)

# host -> {cookie name: value} from the most recent page fetch. Some sites (KVS: data-attach-session)
# expect their session id on download links; it is looked up here and added to the link.
_SESSION_COOKIES = {}
_SESSION_COOKIES_LOCK = threading.Lock()


def _reg_domain_of(host):
    parts = (host or "").lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def _remember_cookies(url, client):
    try:
        host = (urlparse(url).hostname or "").lower()
        jar = {
            c.name: c.value for c in client.cookies.jar
            if c.value and host and (
                host == c.domain.lstrip(".").lower()
                or host.endswith("." + c.domain.lstrip(".").lower())
            )
        }
        if jar:
            with _SESSION_COOKIES_LOCK:
                _SESSION_COOKIES[_reg_domain_of(host)] = jar
    except Exception:
        pass


def session_cookie_for(url, name):
    with _SESSION_COOKIES_LOCK:
        return (_SESSION_COOKIES.get(_reg_domain_of(urlparse(url).netloc)) or {}).get(name)


def fetch_html(url: str, timeout: float = 20.0, referer: str | None = None):
    """Single-shot page fetch (same behaviour as the old working scraper).

    Returns (html, final_url). Raises on network/HTTP failure.
    No multi-library retry loops — those caused long hangs without VPN.
    """
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    to = httpx.Timeout(timeout, connect=min(10.0, timeout))
    r = _FETCH_CLIENT.get(url, headers=headers, timeout=to)
    r.raise_for_status()
    _remember_cookies(str(r.url), _FETCH_CLIENT)
    return r.text or "", str(r.url)


IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|bmp|ico|avif|heic)(\?|#|/|$)", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|webm|ogg|ogv|mov|m4v|mkv|m3u8|mpd)(\?|#|/|$)", re.I)

_PLACEHOLDER_HINTS = re.compile(
    r"(placeholder|lazy|loading|blank|transparent|spacer|1x1|no[-_]?image|preload|"
    r"default[-_]?thumb|/static/img/load|/img/load|load-\dx\d|data:image/gif|"
    r"data:image/svg|pixel\.(?:gif|png)|spacer\.(?:gif|png))",
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
    "/content_video/", "/content_video_alt/",        # pornvideobb-style sites
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
    """Prefer real lazy-load URLs over placeholder src/srcset (common on adult tubes)."""
    def _try(u):
        if _is_acceptable_thumb(u, host) and not _looks_like_placeholder(u):
            return u
        return None

    for v in container.find_all("video"):
        for attr in ("poster", "data-poster", "data-video-poster"):
            hit = _try(_absolute(base, v.get(attr)))
            if hit:
                return hit

    for img in container.find_all("img"):
        for attr in ("data-src", "data-original", "data-lazy", "data-lazy-src",
                     "data-thumb", "data-thumbnail", "data-image", "data-cover",
                     "data-poster", "data-preview", "data-webp", "data-src-retina"):
            hit = _try(_absolute(base, img.get(attr)))
            if hit:
                return hit
        for ss_attr in ("data-srcset", "srcset"):
            hit = _try(_pick_from_srcset(img.get(ss_attr), base))
            if hit:
                return hit
        hit = _try(_absolute(base, img.get("src")))
        if hit:
            return hit

    for s in container.find_all("source"):
        for ss_attr in ("data-srcset", "srcset"):
            hit = _try(_pick_from_srcset(s.get(ss_attr), base))
            if hit:
                return hit
        hit = _try(_absolute(base, s.get("data-src") or s.get("src")))
        if hit:
            return hit

    for el in [container, *container.find_all(style=True)]:
        style = el.get("style") or ""
        m = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", style)
        if m:
            hit = _try(_absolute(base, m.group(1)))
            if hit:
                return hit
    for el in [container, *container.find_all(True)]:
        for attr in ("data-bg", "data-background", "data-bg-src", "data-cover"):
            hit = _try(_absolute(base, el.get(attr)))
            if hit:
                return hit
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

def _unescape_text(text, max_passes=4):
    """Decode HTML entities even when they were escaped more than once
    ('don&amp;#39;t' -> 'don&#39;t' -> "don't"), and drop any tags left inside."""
    import html as _html
    t = text if isinstance(text, str) else ("" if text is None else str(text))
    for _ in range(max_passes):
        u = _html.unescape(t)
        if u == t:
            break
        t = u
    if "<" in t and ">" in t:
        t = re.sub(r"</?[A-Za-z][^<>]{0,200}>", " ", t)      # real tags only, not "I <3 this > that"
    return t.replace("\xa0", " ")


def _clean_title(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", _unescape_text(text)).strip()
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
    for skip in ("/login", "/signup", "/register", "/terms", "/privacy", "/contact", "/about", "/faq", "/dmca"):
        if path.lower().startswith(skip):
            return False
    if re.match(r"^/videos(/(page/)?\d+)?/?$", path.lower()):   # /videos/, /videos/2/ ... are listing pages, not videos
        return False
    cls_id = " ".join(list(a.get("class") or []) + [a.get("id") or ""]).lower()
    if any(k in cls_id for k in ("navbar", "nav-", "-nav", "menu", "header", "footer", "breadcrumb", "pagination")):
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
        return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))
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
# ----------------------------------------------------------------
# CARD METADATA - duration, views, rating, date + every hyperlinked tag on the card
# (genre / pornstar / series / studio / uploader). Works on any card layout: the card is
# found by walking up from the video link until a second, different video link appears.
# ----------------------------------------------------------------
_DUR_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
_NUM_RE = re.compile(r"\d[\d.,]*\s*[kKmMbB]?(?![a-zA-Z])")
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_META_SKIP_TEXT = re.compile(
    r"^(view later|watch later|share|download|play|more|hd|4k|full video|\d+)$", re.I)
_META_TYPES = (
    ("pornstar", r"/(?:pornstars?|models?|actors?|actresses|stars?|performers?|girls?)/", ("icon-star", "pornstar", "model")),
    ("series",   r"/series/",                                                             ("icon-series",)),
    ("studio",   r"/(?:studios?|sites?|networks?|channels?|sponsors?|producers?)/",       ()),
    ("uploader", r"/(?:users?|uploaders?|members?|profile|author|u)/",                     ("info-uploader", "uploader")),
    ("genre",    r"/(?:categor(?:y|ies)|cats?|tags?|genres?|niches?|c)/",                  ("icon-folder", "info-item", "categor", "genre", "tag")),
)

def _reg_domain(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()

def _card_container(a, base, base_host):
    """Largest ancestor of link `a` that still holds only ONE distinct video link."""
    best, node = a, a.parent
    for _ in range(6):
        if node is None or getattr(node, "name", None) in (None, "body", "html", "[document]"):
            break
        keys = set()
        for x in node.find_all("a", href=True):
            if _is_video_preview(x, base_host):
                keys.add(urljoin(base, x["href"]).split("#")[0].split("?")[0])
                if len(keys) > 1:
                    break
        if len(keys) > 1:
            break
        best, node = node, node.parent
    return best

def _class_blob(el, stop=None):
    out, node = [], el
    for _ in range(4):
        if node is None or node is stop or not getattr(node, "get", None):
            break
        out += [c.lower() for c in (node.get("class") or [])]
        node = node.parent
    for sub in el.find_all(True, limit=6):
        out += [c.lower() for c in (sub.get("class") or [])]
    return " ".join(out)

def _classify_meta_link(path, blob):
    for name, path_re, cues in _META_TYPES:
        if re.search(path_re, path, re.I):
            return name
    for name, _, cues in _META_TYPES:
        if any(c in blob for c in cues):
            return name
    return None

def _text_of(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""

def _extract_card_meta(a, base, base_host, video_link):
    """-> dict with only the keys that were found."""
    card = _card_container(a, base, base_host)
    meta, own_key = {}, video_link.split("?")[0].rstrip("/")

    def first(css):
        for sel in css:
            el = card.select_one(sel)
            if el is not None:
                t = _text_of(el)
                if t:
                    return t
        return ""

    dur = first([".duracion", ".duration", "[class*=duration]", "[class*=length]", "time", "[class*=time]"])
    m = _DUR_RE.search(dur)
    if not m:       # fall back to any short element that is just a time
        for el in card.find_all(["span", "div", "em", "i", "b"], limit=40):
            t = _text_of(el)
            if len(t) <= 14 and _DUR_RE.search(t) and not el.find("a"):
                m = _DUR_RE.search(t)
                break
    if m:
        meta["duration"] = m.group(1)

    v = first([".thumb-video-views", ".views", "[class*=views]", "[class*=view-count]"])
    m = _NUM_RE.search(v)
    if m:
        meta["views"] = re.sub(r"\s+", "", m.group(0))

    if "views" not in meta:
        for img in card.find_all("img"):
            key = " ".join([img.get("src") or "", img.get("data-img") or "", img.get("alt") or "",
                            " ".join(img.get("class") or [])]).lower()
            if re.search(r"eye|views?", key) and img.parent is not None:
                mv = _NUM_RE.search(_text_of(img.parent))
                if mv:
                    meta["views"] = re.sub(r"\s+", "", mv.group(0))
                    break

    r = first([".rating", "[class*=rating]", "[class*=likes]", "[class*=percent]"])
    m = _PCT_RE.search(r)
    if m:
        meta["rating"] = m.group(1) + "%"

    d = first(["[class*=added]", "[class*=date]", "[class*=ago]"])
    tm = card.find("time")
    if tm is not None and (tm.get("datetime") or _text_of(tm)):
        d = _text_of(tm) or tm.get("datetime")
    if d and len(d) <= 30 and not _DUR_RE.fullmatch(d):
        meta["added"] = d

    q = first([".hd", ".is-hd", "[class*=quality]", "[class*=resolution]"])
    if q and len(q) <= 8:
        meta["quality"] = q.upper()

    links, seen = [], {own_key}
    for x in card.find_all("a", href=True):
        href = (x.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base, href).split("#")[0]
        key = full.split("?")[0].rstrip("/")
        if key in seen:
            continue
        pu = urlparse(full)
        if _reg_domain(pu.netloc) != _reg_domain(base_host) or pu.path in ("", "/"):
            continue
        kind = _classify_meta_link(pu.path.lower(), _class_blob(x, stop=card))
        if not kind:
            continue
        img = x.find("img")
        label = _text_of(x) or (img.get("alt") if img else "") or x.get("title") or ""
        label = re.sub(r"\s+", " ", label).strip()
        if not (2 <= len(label) <= 80) or _META_SKIP_TEXT.match(label):
            continue
        seen.add(key)
        links.append({"type": kind, "label": label, "link": full})
        if len(links) >= 12:
            break
    if links:
        meta["meta_links"] = links
        genres = [l["label"] for l in links if l["type"] == "genre"]
        if genres:
            meta["genres"] = genres
    return meta


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
        try:
            raw_items[-1].update(_extract_card_meta(a, base, base_host, full))
        except Exception:
            pass        # metadata is a bonus; never lose the video because of it
        if len(raw_items) >= max_items:
            break
    thumb_counts = Counter(it["thumbnail"] for it in raw_items if it.get("thumbnail"))
    for it in raw_items:
        th = it.get("thumbnail")
        if th and thumb_counts[th] >= 3:
            it["thumbnail"] = None
    return raw_items

def scrape_page(url: str, max_items: int = 80, page_num: None = None, timeout: float = 25.0):
    try:
        html, base = fetch_html(url, timeout=timeout)
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

def _card_for_link(a):
    """Walk up parents to find a card that holds the thumbnail (may be sibling of the link)."""
    CARD_TOKENS = {
        "channel", "channels", "categories_card", "category-card", "category_card",
        "item", "card", "thumb", "studio", "model", "site-card", "sponsor",
        "thumb-serie", "categories_card_body",
    }
    candidates = []
    node = a.parent if a is not None else None
    for _ in range(6):
        if node is None or not getattr(node, "get", None):
            break
        tokens = {c.lower() for c in (node.get("class") or [])}
        joined = " ".join(tokens)
        hit = bool(tokens & CARD_TOKENS) or any(
            k in joined for k in (
                "categories_card", "category-card", "category_card",
                "site-card", "thumb-serie",
            )
        )
        if hit:
            candidates.append(node)
        node = node.parent
    for c in candidates:
        if c.find("img") or c.find("video"):
            return c
    return candidates[-1] if candidates else a


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
    card = _card_for_link(a)
    has_image = bool(a.find("img")) or bool(a.find("video")) or bool(card.find("img")) or bool(card.find("video"))
    path_ok = bool(re.search(
        r"/(?:categor(?:y|ies)|cat|tags?|c|video-category|studio|studios|"
        r"channel|channels|model|models|site|sites|network|networks|"
        r"series|sponsor|pornstar)/",
        path,
    ))
    if not has_image and not path_ok:
        return False
    cls_id = " ".join(list(a.get("class") or []) + [a.get("id") or ""]).lower()
    if any(k in cls_id for k in CATEGORY_SKIP_CLASS_HINTS):
        return False
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
        if re.match(r"^/videos(/\d+)?$", path):   # "all videos" listing and its pages are not categories/studios
            continue
        if re.match(r"^/[a-z]{2}(/|$)", path) and (path.endswith("/sites") or path.endswith("/categories") or path.endswith("/cats")):
            continue
        key = full.split("?")[0]
        name = (_clean_category_name(_find_title(a, base))
                or _clean_category_name(_title_from_url(full)))
        if not name:
            continue
        card = _card_for_link(a)
        thumb = _find_thumbnail(a, base, host=base_host) or _find_thumbnail(card, base, host=base_host)
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
        html, base = fetch_html(url, timeout=25.0)
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
        while next_url and pages_read < (max_pages if is_sites else 1) and next_url not in seen_pages:
            seen_pages.add(next_url)
            html, base = fetch_html(next_url, timeout=20.0)
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

_LAZY_IMG_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-webp",
                   "data-thumb", "data-savepage-src", "src")

def _img_url(img, base):
    """Real image URL of a (possibly lazy-loaded) <img>; ignores data: placeholders."""
    if img is None:
        return None
    for attr in _LAZY_IMG_ATTRS:
        v = (img.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return urljoin(base, v)
    for attr in ("data-srcset", "srcset"):
        v = img.get(attr)
        if v:
            try:
                got = _pick_from_srcset(v, base)
            except Exception:
                got = None
            if got:
                return got
    return None

def _extract_studio_video(item, base):
    a = item.find("a", href=True)
    if not a:
        return None
    link = urljoin(base, a["href"].split("#")[0])
    title = (a.get("title") or "").strip()
    if not title:
        strong = item.select_one(".item-info .title, .title, strong")
        title = strong.get_text(" ", strip=True) if strong else ""
    img = item.select_one("img")
    if not title and img is not None:
        title = (img.get("alt") or "").strip()
    if not title:
        title = _title_from_url(link)
    thumb = _img_url(img, base)
    prev_el = item.select_one("[data-preview]")
    preview = prev_el.get("data-preview") if prev_el else None
    dur_el = item.select_one(".duration")
    duration = dur_el.get_text(" ", strip=True) if dur_el else None
    out = {"title": title, "link": link, "thumbnail": thumb,
           "duration": duration, "preview": preview}
    dm = _DUR_RE.search(duration or "")
    if dm:
        out["duration"] = dm.group(1)
    vw = item.select_one(".views")
    vm = _NUM_RE.search(vw.get_text(" ", strip=True)) if vw else None
    if vm:
        out["views"] = re.sub(r"\s+", "", vm.group(0))
    rt = item.select_one(".rating")
    rm = _PCT_RE.search(rt.get_text(" ", strip=True)) if rt else None
    if rm:
        out["rating"] = rm.group(1) + "%"
    return out

def _extract_superporn_series(soup, base, max_sections=100):
    """Superporn /series cards: .thumb-serie > a + img."""
    sections, seen = [], set()
    cards = soup.select(".thumb-serie") or soup.select("[class*='thumb-serie']")
    for card in cards:
        a = card.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        link = urljoin(base, href.split("#")[0])
        key = link.split("?")[0].rstrip("/")
        if key in seen:
            continue
        img = card.find("img") or a.find("img")
        name = None
        if img and img.get("alt"):
            name = _clean_category_name(img.get("alt"))
        if not name:
            name = _clean_category_name(a.get("title") or a.get("aria-label"))
        if not name:
            name = _clean_category_name(_title_from_url(link))
        if not name:
            continue
        thumb = _find_thumbnail(card, base) or _find_thumbnail(a, base)
        seen.add(key)
        sections.append({
            "name": name, "link": link, "thumbnail": thumb,
            "video_count": None, "videos": [],
        })
        if len(sections) >= max_sections:
            break
    return sections


_STUDIO_PATH_RE = re.compile(r"/(?:sites?|studios?|channels?|networks?|sponsors?)/[^/?#]+", re.I)

def _extract_studio_sections(soup, base, max_sections=100):
    """Studio blocks as the /sites/ page lists them:
        .headline (name + "See All N Videos" link)  ->  .list-videos (preview cards)
    Walks headlines and video grids in DOCUMENT ORDER and attaches every grid to the
    closest preceding headline, so extra wrappers / ad blocks between them don't matter."""
    series = _extract_superporn_series(soup, base, max_sections=max_sections)
    if series:
        return series

    container = (soup.select_one("#list_content_sources_sponsors_list_items")
                 or soup.select_one("[id^='list_content_sources']")
                 or soup.select_one("main, #content, .content")
                 or soup)
    sections, current, seen = [], None, set()
    for el in container.select(".headline, .list-videos"):
        classes = el.get("class") or []
        if "headline" in classes:
            current = None
            more = el.find("a", class_="more") or next(
                (x for x in el.find_all("a", href=True) if _STUDIO_PATH_RE.search(x["href"])), None)
            if not more or not more.get("href"):
                continue
            link = urljoin(base, more["href"].split("#")[0])
            if not _STUDIO_PATH_RE.search(urlparse(link).path):
                continue
            h = el.find(["h1", "h2", "h3", "h4"])
            name = (h.get_text(" ", strip=True) if h else "") or (el.get("title") or "")
            if not name:
                name = _clean_category_name(_title_from_url(link)) or ""
            key = link.split("?")[0].rstrip("/")
            if not name or key in seen:
                continue
            label = more.get_text(" ", strip=True)
            seen.add(key)
            current = {"name": name, "link": link,
                       "video_count": _clean_video_count(label),
                       "see_more_label": label or "See all",
                       "videos": [], "thumbnail": None}
            sections.append(current)
            if len(sections) >= max_sections:
                break
        elif current is not None:
            for item in el.select(".item"):
                v = _extract_studio_video(item, base)
                if v and all(v["link"] != x["link"] for x in current["videos"]):
                    current["videos"].append(v)
    for s in sections:
        s["thumbnail"] = next((v["thumbnail"] for v in s["videos"] if v.get("thumbnail")), None)

    if sections:
        return sections

    # Fallback: plain studio grid (no preview videos) - cards linking to /sites/<slug>/
    for a in container.find_all("a", href=True):
        link = urljoin(base, a["href"].split("#")[0])
        path = urlparse(link).path
        if not _STUDIO_PATH_RE.search(path) or re.fullmatch(r"/sites?/\d*/?", path):
            continue
        key = link.split("?")[0].rstrip("/")
        if key in seen:
            continue
        img = a.find("img")
        name = _clean_category_name(a.get("title") or (img.get("alt") if img else "")
                                    or a.get_text(" ", strip=True)
                                    or _title_from_url(link))
        if not name:
            continue
        seen.add(key)
        sections.append({"name": name, "link": link, "thumbnail": _img_url(img, base),
                         "video_count": _clean_video_count(a.get_text(" ", strip=True)),
                         "see_more_label": "See all", "videos": []})
        if len(sections) >= max_sections:
            break
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
        html, base = fetch_html(url, timeout=25.0)
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

# ----------------------------------------------------------------
# SUPERPORN CATEGORIES  (https://www.superporn.com/categories, paginated /categories/2 ...)
# Every card is one <a> that holds the thumbnail, the name and "1,234 videos".
# ----------------------------------------------------------------
_SP_SKIP_SLUGS = {"categories", "series", "pornstars", "login", "signup", "upload", "contact",
                  "tos", "dmca", "cookies-policy", "search", "live", "es", "de", "it", "fr", "br", "nl"}
_COUNT_TAIL = re.compile(r"([\d][\d,\.]*)\s*(?:videos?|vids?|clips?)\s*$", re.I)

def _collapse_repeat(name):
    """'Lesbian Lesbian' (img alt + caption) -> 'Lesbian'."""
    w = (name or "").split()
    n = len(w)
    if n >= 2 and n % 2 == 0 and [x.lower() for x in w[:n // 2]] == [x.lower() for x in w[n // 2:]]:
        return " ".join(w[:n // 2])
    return name

def _extract_superporn_categories(soup, base, max_items=200):
    host = urlparse(base).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True)
        m = _COUNT_TAIL.search(txt)
        if not m:
            continue
        link = urljoin(base, a["href"].split("#")[0])
        p = urlparse(link)
        if host and p.netloc != host:
            continue
        slug = p.path.strip("/")
        if not slug or "/" in slug or slug.lower() in _SP_SKIP_SLUGS:
            continue
        key = link.split("?")[0].rstrip("/")
        if key in seen:
            continue
        img = a.find("img")
        name = _clean_category_name(_collapse_repeat(txt[:m.start()].strip())) \
            or _clean_category_name(img.get("alt") if img else None) \
            or _clean_category_name(_title_from_url(link))
        if not name:
            continue
        seen.add(key)
        out.append({
            "name": name, "link": link,
            "thumbnail": _img_url(img, base) or _find_thumbnail(a, base, host=host),
            "video_count": _clean_video_count(m.group(1)),
            "uid": hashlib.md5(link.encode("utf-8")).hexdigest()[:12],
        })
        if len(out) >= max_items:
            break
    return out

def scrape_superporn_categories(url: str = "https://www.superporn.com/categories",
                                max_items: int = 200, page_num=None):
    try:
        html, base = fetch_html(url, timeout=25.0)
    except Exception as e:
        return {"page": url, "categories": [], "count": 0, "error": str(e),
                "next_page": None, "page_num": page_num or _current_page_number(url),
                "kind": "categories"}
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base).netloc
    cats = _extract_superporn_categories(soup, base, max_items=max_items)
    if not cats:
        cats = _extract_categories_from_soup(soup, base, max_items=max_items)
    pn = page_num or _current_page_number(base)
    # Pagination is explicit on this site (1 2 3 ... Next). Never guess past the last page.
    nxt = _find_next_page_link(soup, base, base_host) if cats else None
    if nxt and _current_page_number(nxt) <= pn:
        nxt = None
    title = soup.title.string.strip() if soup.title and soup.title.string else base
    out = {"page": base, "page_title": title, "categories": cats, "count": len(cats),
           "next_page": nxt, "next_is_guess": False, "page_num": pn, "kind": "categories"}
    if not cats:
        out["error"] = f"No categories found in the page the server received ({len(html)} bytes)."
    return out


# ----------------------------------------------------------------
# MODELS / PORNSTARS  (https://www.freesexvideos.xxx/models/, /models/2/ ...)
# ----------------------------------------------------------------
_MODEL_HREF_RE = re.compile(r"/(?:models?|pornstars?|actresses)/([^/?#]+)/?$", re.I)

def _extract_models(soup, base, max_items=200):
    host = urlparse(base).netloc
    out, seen = [], set()
    root = (soup.select_one("#list_models_models_list_items")
            or soup.select_one("[id^='list_models']")
            or soup.select_one(".list-models")
            or soup)
    for a in root.find_all("a", href=True):
        link = urljoin(base, a["href"].split("#")[0])
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
        for _ in range(3):      # climb to the card that holds img + title + count
            if card.find("img") and (card.name != "a" or card.get_text(strip=True)):
                break
            if card.parent is None or card.parent.name in ("body", "html", "[document]"):
                break
            card = card.parent
        img = a.find("img") or card.find("img")
        title_el = card.select_one(".title, strong, h2, h3")
        name = _clean_category_name(
            (a.get("title") or "")
            or (title_el.get_text(" ", strip=True) if title_el else "")
            or (img.get("alt") if img else "")
            or _title_from_url(link))
        if not name:
            continue
        ctext = card.get_text(" ", strip=True)
        cm = re.search(r"([\d][\d,]*)\s*(?:videos?|vids?|clips?)", ctext, re.I)
        seen.add(key)
        out.append({
            "name": name, "link": link,
            "thumbnail": _img_url(img, base),
            "video_count": _clean_video_count(cm.group(1)) if cm else None,
            "uid": hashlib.md5(link.encode("utf-8")).hexdigest()[:12],
        })
        if len(out) >= max_items:
            break
    return out

def scrape_models(url: str = "https://www.freesexvideos.xxx/models/", max_items: int = 200, page_num=None):
    try:
        html, base = fetch_html(url, timeout=25.0)
    except Exception as e:
        return {"page": url, "categories": [], "models": [], "count": 0, "error": str(e),
                "next_page": None, "page_num": page_num or _current_page_number(url),
                "kind": "pornstars"}
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base).netloc
    models = _extract_models(soup, base, max_items=max_items)
    pn = page_num or _current_page_number(base)
    nxt = _find_next_page_link(soup, base, base_host) if models else None
    explicit = bool(nxt)
    if models and not nxt:
        nxt = _guess_next_page(base, pn)
    title = soup.title.string.strip() if soup.title and soup.title.string else base
    out = {"page": base, "page_title": title,
           "categories": models, "models": models,   # "categories" keeps the existing grid renderer working
           "count": len(models), "next_page": nxt, "next_is_guess": bool(nxt) and not explicit,
           "page_num": pn, "kind": "pornstars"}
    if not models:
        out["error"] = f"No models found in the page the server received ({len(html)} bytes)."
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
        r'["\'](?:hls|mp4|source)["\']\s*:\s*["\']([^"\']+?\.(?:mp4|m3u8|webm|mov|mpd)[^"\']*)["\']',
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
        html, base = fetch_html(detail_url, timeout=(10 if light else 25))
    except Exception as e:
        msg = str(e)
        unavailable = any(x in msg for x in ("404", "410"))
        return {"error": msg, "detail_url": detail_url, "unavailable": unavailable,
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
    downloads = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        full = urljoin(base, href)
        low = full.lower()
        if _classify(full) not in ("mp4", "webm") or _is_image_url(full):
            continue
        cls = " ".join(a.get("class") or []).lower()
        if "download=" in low or a.has_attr("download") or "download" in cls:
            downloads.append({"src": full, "label": a.get_text(" ", strip=True)[:40],
                              "attach_session": a.get("data-attach-session") or None})
    result = {
        "detail_url": detail_url,
        "thumbnail": _extract_detail_thumbnail(soup, base, host=host),
        "video": out[0] if out else None,
        "all": out,
        "downloads": downloads,
        # page loaded fine but no playable media anywhere = deleted / dead video
        "unavailable": (not out) and (not iframe_failed),
    }
    if out or not iframe_failed:
        _RESOLVE_CACHE[ckey] = (now, result)
    return result

# ----------------------------------------------------------------
# search (modified for speed)
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

_SEARCH_CACHE = {}
_SEARCH_TTL = 300
_SEARCH_MAX = 512
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_URL_PREFERENCE = {}
_SEARCH_PREFERENCE_MAX = 256


def search_one(site, query, max_items=80):
    """Use a site's previously successful search URL first, falling back to other shapes."""
    ckey = (site, query, max_items)
    now = time.time()
    with _SEARCH_CACHE_LOCK:
        hit = _SEARCH_CACHE.get(ckey)
        preferred = _SEARCH_URL_PREFERENCE.get(site)
    if hit and now - hit[0] < _SEARCH_TTL:
        return copy.deepcopy(hit[1])

    urls = _build_search_urls(site, query)

    def work(i, url):
        try:
            res = scrape_page(url, max_items=max_items, timeout=7.0)
        except Exception as e:
            res = {"page": url, "items": [], "count": 0, "error": str(e),
                   "next_page": None}
        res["query"] = query
        res["site"] = site
        res["search_url"] = url
        return i, res

    results = [None] * len(urls)
    preferred_hit = False
    if preferred is not None and 0 <= preferred < len(urls):
        i, result = work(preferred, urls[preferred])
        results[i] = result
        preferred_hit = bool(result.get("items"))

    if not preferred_hit:
        candidates = [(i, url) for i, url in enumerate(urls) if results[i] is None]
        # Race all URL shapes, but do NOT wait for the slow/dead ones: as soon as a shape with
        # results is in and every higher-priority shape has answered (or a short grace has passed),
        # take it. Before, one dead shape cost the full 7s timeout on every first search.
        pool = ThreadPoolExecutor(max_workers=min(6, len(candidates)))
        try:
            futures = [pool.submit(work, i, url) for i, url in candidates]
            first_hit_at = None
            pending = set(futures)
            while pending:
                done_now = [f for f in pending if f.done()]
                for f in done_now:
                    pending.discard(f)
                    i, result = f.result()
                    results[i] = result
                    if result.get("items") and first_hit_at is None:
                        first_hit_at = time.time()
                if first_hit_at is not None:
                    best = min((i for i, r in enumerate(results) if r and r.get("items")), default=None)
                    higher_pending = any(results[i] is None for i in range(best))
                    if not higher_pending or time.time() - first_hit_at > 0.6:
                        break
                if pending and not done_now:
                    time.sleep(0.02)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)   # leftover requests finish in the background

    out = next((r for r in results if r and (r.get("items") or [])), None) or results[0]
    if out is not None:
        if out.get("items"):
            out["source"] = "site-search"
            winning_index = urls.index(out.get("search_url"))
            with _SEARCH_CACHE_LOCK:
                if site not in _SEARCH_URL_PREFERENCE and len(_SEARCH_URL_PREFERENCE) >= _SEARCH_PREFERENCE_MAX:
                    _SEARCH_URL_PREFERENCE.pop(next(iter(_SEARCH_URL_PREFERENCE)))
                _SEARCH_URL_PREFERENCE[site] = winning_index
        else:
            out.setdefault("source", "none")
        with _SEARCH_CACHE_LOCK:
            expired = [key for key, value in _SEARCH_CACHE.items() if now - value[0] >= _SEARCH_TTL]
            for key in expired:
                _SEARCH_CACHE.pop(key, None)
            if len(_SEARCH_CACHE) >= _SEARCH_MAX:
                oldest = min(_SEARCH_CACHE, key=lambda key: _SEARCH_CACHE[key][0])
                _SEARCH_CACHE.pop(oldest, None)
            _SEARCH_CACHE[ckey] = (now, out)
    return copy.deepcopy(out)

def _dedupe_items(results):
    """Drop videos that appear on more than one site (embeds/reposts) — the first
    site that returned the video keeps it, in site-priority order."""
    seen = set()
    for res in results:
        items = res.get("items") or []
        deduped = []
        for it in items:
            k = (it.get("link") or "").split("?")[0]
            if not k or k in seen:
                continue
            seen.add(k)
            deduped.append(it)
        res["items"] = deduped
        res["count"] = len(deduped)
    return results


def _verify_item(item):
    """Light availability check: a video whose page loads but exposes no media is
    deleted/dead and only pollutes search results."""
    try:
        r = resolve_video_url(item["link"], light=True)
        return item, not (r.get("unavailable") and not r.get("video"))
    except Exception:
        return item, True


def search_many(sites, query, max_items=80, verify=False):
    results_map = {}
    # increase parallelism but keep it bounded so we don't exhaust file descriptors / memory
    workers = min(3, max(1, len(sites)))
    def work(site):
        try:
            res = search_one(site, query, max_items=max_items)
            return site, res
        except Exception as e:
            return site, {"page": site, "site": site, "query": query,
                          "items": [], "count": 0, "error": str(e),
                          "next_page": None, "source": "none"}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in as_completed([pool.submit(work, s) for s in sites]):
            s, res = f.result()
            results_map[s] = res
    results = [results_map[s] for s in sites]
    results = _dedupe_items(results)
    if verify:
        # bounded concurrency: one light resolve per item, dead videos dropped
        for res in results:
            items = res.get("items") or []
            if not items:
                continue
            keep = []
            with ThreadPoolExecutor(max_workers=4) as vpool:
                for item, ok in vpool.map(_verify_item, items[:40]):
                    if ok:
                        keep.append(item)
            res["items"] = keep
            res["count"] = len(keep)
            if items and not keep:
                res["all_dead"] = True
    return results