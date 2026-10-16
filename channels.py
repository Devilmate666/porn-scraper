"""Live TV channels from xlivetv.com: channel cards (title, thumbnail, description, categories) with paging.

xlivetv.com is a static Jekyll site, so plain HTML is enough (no JavaScript rendering needed):
    /                       channel grid, 12 per page      /page/N/           more pages
    /categories/            category list with counts      /categories/<slug>/  channels of one category
    /<slug>/                one channel: title, description, categories, og:image (its logo)

fetch_channels(category=None, page=1) -> {items, page, next_page, total_pages, categories, diagnostics}
Every item: title, link, slug, thumbnail, thumbnails[], description, categories[{name, slug}], provider.
The listing gives title + link (+ image when the card has one); each channel page is then read once, in
parallel and cached for an hour, to add description, categories and the og:image as a thumbnail fallback.
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

BASE = "https://xlivetv.com/"
HOST = "xlivetv.com"
_NON_CHANNEL = {"categories", "favorites", "games", "page", "contact", "terms-of-use", "privacy-policy",
                "disclaimer", "assets", "cdn-cgi", "search", "about", "dmca", "sitemap", "sitemap.xml",
                "feed.xml", "robots.txt", "tag", "tags", "blog", "favicon.ico"}
_CAT_RX = re.compile(r"^[a-z0-9][a-z0-9\-]{0,60}$")
_PAGE_RX = re.compile(r"/page/(\d+)/?$")
_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-lazy", "data-thumb", "data-image", "data-bg",
          "data-background", "src")

_PAGE_CACHE, _INFO_CACHE, _CAT_CACHE = {}, {}, {"ts": 0.0, "items": []}
_PAGE_TTL, _INFO_TTL, _CAT_TTL = 300, 3600, 3600
_LOCK = threading.Lock()


# ----------------------------------------------------------------------------- helpers
def _abs(base, u):
    u = (u or "").strip()
    if not u or u.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    return urljoin(base, u).split("#")[0]


def _same_host(url):
    h = (urlparse(url).hostname or "").lower()
    return h == HOST or h.endswith("." + HOST)


def _segments(url):
    return [s for s in urlparse(url).path.split("/") if s]


def _channel_slug(url):
    """'https://xlivetv.com/brazzers-tv-online/' -> 'brazzers-tv-online' (None for pages that are not channels)."""
    if not url or not _same_host(url):
        return None
    seg = _segments(url)
    return seg[0] if len(seg) == 1 and seg[0].lower() not in _NON_CHANNEL and "." not in seg[0] else None


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el is not None else ""


def _pretty(slug):
    return " ".join(w.upper() if w in ("tv", "hd", "xxx") else w.capitalize() for w in slug.split("-"))


def _from_srcset(v, base):
    best, bw = None, -1
    for part in (v or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        m = re.match(r"(\d+)", bits[1]) if len(bits) > 1 else None
        w = int(m.group(1)) if m else 0
        if w > bw:
            best, bw = bits[0], w
    return _abs(base, best) if best else None


def _images_in(container, base):
    """Every plausible image of a card, best guess first: <img>, <source>, CSS backgrounds, data-bg."""
    out = []

    def add(u):
        u = _abs(base, u)
        if u and not re.search(r"hits\.sh|favicon|pixel|spacer|blank", u, re.I) and u not in out:
            out.append(u)

    for img in container.find_all("img"):
        for a in _ATTRS[:-1]:
            add(img.get(a))
        add(_from_srcset(img.get("data-srcset") or img.get("srcset"), base))
        add(img.get("src"))
    for so in container.find_all("source"):
        add(_from_srcset(so.get("data-srcset") or so.get("srcset"), base))
    for el in [container, *container.find_all(True)]:
        m = re.search(r"url\(\s*['\"]?([^'\")]+)", el.get("style") or "")
        if m:
            add(m.group(1))
        for a in ("data-bg", "data-background", "data-bg-src", "data-image"):
            add(el.get(a))
    return out


def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and (tag.get("content") or "").strip():
            return tag["content"].strip()
    return None


# ----------------------------------------------------------------------------- listing page
def _card_container(anchors):
    """Lowest ancestor that holds every anchor of the same channel: that is the card."""
    first = anchors[0]
    for anc in first.parents:
        if anc.name in ("body", "html", "[document]"):
            break
        if all(anc in a.parents or anc is a for a in anchors[1:]):
            return anc
    return first.parent


def parse_listing(html, base=BASE):
    """Channel grid page -> (items, max_page)."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    groups = {}
    for a in soup.select("a[href]"):
        link = _abs(base, a.get("href"))
        if link and _channel_slug(link):
            groups.setdefault(link.split("?")[0].rstrip("/") + "/", []).append(a)

    items = []
    for link, anchors in groups.items():
        slug = _channel_slug(link)
        card = _card_container(anchors)
        head = card.find(["h2", "h3", "h4"]) if card is not None else None
        title = _txt(head)
        if not title:
            for a in anchors:
                t = (a.get("title") or a.get("aria-label") or "").strip()
                img = a.find("img")
                t = t or ((img.get("alt") or "").strip() if img else "")
                t = t or (_txt(a) if _txt(a).lower() not in ("watch now", "watch", "play") else "")
                if t:
                    title = t
                    break
        title = title or _pretty(slug)
        imgs = _images_in(card, base) if card is not None else []
        cats = []
        if card is not None:
            for ca in card.select('a[href*="/categories/"]'):
                cs = (_segments(_abs(base, ca.get("href")) or "") or [None, None])[-1]
                if cs and _CAT_RX.match(cs) and cs != "categories":
                    cats.append({"name": _pretty(cs), "slug": cs})
        items.append({"title": title, "link": link, "slug": slug, "thumbnail": imgs[0] if imgs else None,
                      "thumbnails": imgs[:3], "description": None, "categories": cats, "provider": "xlivetv"})

    pages = [int(m.group(1)) for a in soup.select("a[href]") for m in [_PAGE_RX.search(a.get("href") or "")] if m]
    return items, max(pages) if pages else 1


# ----------------------------------------------------------------------------- channel page
def parse_channel(html, url):
    """One channel page -> {title, image, description, categories[]}."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    h1 = soup.find("h1")
    desc = None
    root = (h1.find_parent(["main", "article", "section"]) if h1 else None) or soup.body or soup
    paras = [p for p in root.find_all("p") if p.find_parent(["footer", "nav", "header"]) is None]
    best = ""
    for p in paras:
        t = _txt(p)
        if len(t) > len(best) and not re.search(r"please wait|cookie|all rights reserved|start typing", t, re.I):
            best = t
    desc = best or _meta(soup, "og:description", "description")
    if desc and len(desc) < 25 and len(_meta(soup, "og:description", "description") or "") > len(desc):
        desc = _meta(soup, "og:description", "description")
    cats, seen = [], set()
    for a in soup.select('a[href*="/categories/"]'):
        if a.find_parent(["footer", "nav"]) is not None:
            continue
        cs = (_segments(_abs(url, a.get("href")) or "") or [None])[-1]
        if cs and cs != "categories" and _CAT_RX.match(cs) and cs not in seen:
            seen.add(cs)
            cats.append({"name": _pretty(cs), "slug": cs})
    return {"title": _txt(h1) or None, "image": _abs(url, _meta(soup, "og:image", "twitter:image")),
            "description": desc, "categories": cats}


def _channel_info(url):
    hit = _INFO_CACHE.get(url)
    if hit and time.time() - hit[0] < _INFO_TTL:
        return hit[1]
    from scraper import fetch_html            # the app's HTTP client (headers, DNS fallbacks, timeouts)
    html, final = fetch_html(url, timeout=12.0, referer=BASE)
    info = parse_channel(html, final)
    _INFO_CACHE[url] = (time.time(), info)
    if len(_INFO_CACHE) > 600:
        for k in sorted(_INFO_CACHE, key=lambda k: _INFO_CACHE[k][0])[:200]:
            _INFO_CACHE.pop(k, None)
    return info


def _enrich(items, diag):
    """Read every channel page once (parallel): description, categories, og:image as thumbnail fallback."""
    if not items:
        return
    ex = ThreadPoolExecutor(max_workers=6)
    futs = {ex.submit(_channel_info, it["link"]): it for it in items}
    ok = 0
    try:
        for f in as_completed(futs, timeout=25):
            it = futs[f]
            try:
                info = f.result()
            except Exception as e:
                diag.append(f"channel {it['slug']}: {type(e).__name__}: {str(e)[:80]}")
                continue
            ok += 1
            it["description"] = info.get("description") or it.get("description")
            if info.get("categories"):
                it["categories"] = info["categories"]
            if info.get("title") and len(info["title"]) >= 2:
                it["title"] = info["title"]
            if info.get("image") and info["image"] not in it["thumbnails"]:
                it["thumbnails"].append(info["image"])
            it["thumbnail"] = it["thumbnails"][0] if it["thumbnails"] else None
    except Exception:
        diag.append("channel pages: some did not answer in time")
    ex.shutdown(wait=False, cancel_futures=True)
    diag.append(f"channel pages read: {ok}/{len(items)}")


# ----------------------------------------------------------------------------- categories
def fetch_channel_categories():
    if _CAT_CACHE["items"] and time.time() - _CAT_CACHE["ts"] < _CAT_TTL:
        return _CAT_CACHE["items"]
    from scraper import fetch_html
    html, final = fetch_html(BASE + "categories/", timeout=15.0, referer=BASE)
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.select('a[href*="/categories/"]'):
        url = _abs(final, a.get("href"))
        seg = _segments(url or "")
        if not url or not _same_host(url) or len(seg) != 2 or seg[0] != "categories" or seg[1] in seen:
            continue
        text = _txt(a)
        m = re.match(r"^(.*?)\s*(\d+)\s*channels?$", text, re.I)
        seen.add(seg[1])
        out.append({"slug": seg[1], "name": _pretty(seg[1]), "count": int(m.group(2)) if m else None})
    out.sort(key=lambda c: -(c["count"] or 0))
    if out:
        _CAT_CACHE.update(ts=time.time(), items=out)
    return out


# ----------------------------------------------------------------------------- public entry point
def _page_url(category, page):
    base = BASE if not category else f"{BASE}categories/{category}/"
    return base if page <= 1 else f"{base}page/{page}/"


def fetch_channels(category=None, page=1, force=False):
    category = (category or "").strip().lower() or None
    if category and not _CAT_RX.match(category):
        return {"items": [], "count": 0, "error": "invalid category", "diagnostics": []}
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    key = (category, page)
    hit = _PAGE_CACHE.get(key)
    if hit and not force and time.time() - hit[0] < _PAGE_TTL:
        return hit[1]

    from scraper import fetch_html
    diag, url = [], _page_url(category, page)
    try:
        html, final = fetch_html(url, timeout=20.0, referer=BASE)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if page > 1 and status == 404:                       # walked past the last page
            return {"page": page, "items": [], "count": 0, "next_page": None, "total_pages": page - 1,
                    "category": category, "diagnostics": [f"{url}: 404"]}
        return {"page": page, "items": [], "count": 0, "next_page": None, "category": category,
                "error": f"Could not load {url}: {type(e).__name__}: {str(e)[:120]}", "diagnostics": diag}
    items, last = parse_listing(html, final)
    diag.append(f"{url}: {len(items)} channels, last page {last}")
    _enrich(items, diag)
    for it in items:
        it["thumbnails"] = [u for u in it["thumbnails"] if u][:4]
        it["thumbnail"] = it["thumbnails"][0] if it["thumbnails"] else None
    try:                                                      # let the thumbnail proxy serve these images too
        from livecams import register_thumb_urls
        register_thumb_urls([u for it in items for u in it["thumbnails"]], "xlivetv")
    except Exception:
        pass
    cats = []
    if page == 1:
        try:
            cats = fetch_channel_categories()
        except Exception as e:
            diag.append(f"categories: {type(e).__name__}: {str(e)[:80]}")
    out = {"page": page, "items": items, "count": len(items), "category": category,
           "total_pages": last, "next_page": page + 1 if (items and page < last) else None,
           "categories": cats, "diagnostics": diag, "fetched_at": int(time.time())}
    if items:
        _PAGE_CACHE[key] = (time.time(), out)
    return out
