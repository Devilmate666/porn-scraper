#!/usr/bin/env python3
"""
Cloudflare KV Scraper - Runs in GitHub Actions, writes scraped data to Cloudflare KV.
This replaces the in-memory caching and threading from the original Flask app.
"""

import os
import re
import sys
import json
import time
import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote, quote_plus

import httpx
import requests

# Add local backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import (
    scrape_page, scrape_categories, scrape_tags, scrape_studio_sections,
    scrape_superporn_categories, scrape_models, search_many, fetch_html,
    _current_page_number, _guess_next_page, _find_next_page_link,
)
# Frontend SOURCES (from index.html) - these are what the frontend expects
FRONTEND_SOURCES = [
    "https://www.superporn.com/",
    "https://2023.pornvideobb.com/",
    "https://www.freesexvideos.xxx/",
    "https://www.bdsmhole.com/",
]

# Also include TEST_SITES for search fallback
from sourcetest import TEST_SITES, search_site, site_from_url
from extras import scrape_plus, deep_resolve, is_smart
try:
    from livecams import fetch_livecams
except Exception:
    fetch_livecams = None
try:
    from channels import fetch_channels
except Exception:
    fetch_channels = None
try:
    from metadata import fetch_metadata
except Exception:
    fetch_metadata = None


# Cloudflare KV API client
class KVClient:
    def __init__(self, account_id: str, api_token: str, namespace_id: str):
        self.account_id = account_id
        self.api_token = api_token
        self.namespace_id = namespace_id
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def put(self, key: str, value: dict, expiration_ttl: int = 3600, skip_same: bool = True) -> bool:
        """Write to KV with TTL in seconds. Key MUST be URL-encoded (it contains '/' and ':').
        Identical existing values are left alone (reads are free, writes are limited)."""
        if skip_same:
            try:
                if self.get(key) == value:
                    return True
            except Exception:
                pass
        url = f"{self.base_url}/values/{quote(key, safe='')}"
        params = {"expiration_ttl": max(expiration_ttl, 60)} if expiration_ttl else {}
        resp = self.session.put(url, data=json.dumps(value), params=params)
        if resp.status_code not in (200, 201):
            print(f"  KV PUT FAILED {resp.status_code} key={key[:90]} body={resp.text[:200]}", file=sys.stderr)
            return False
        return True

    def put_batch(self, entries: list[tuple[str, dict]], expiration_ttl: int = 3600) -> int:
        """Write multiple entries. Returns success count."""
        success = 0
        for key, value in entries:
            if self.put(key, value, expiration_ttl):
                success += 1
            time.sleep(0.05)  # Rate limit
        return success

    def get(self, key: str) -> dict | None:
        """None = key does not exist. Raises on any other failure (so callers never mistake an outage for 'empty')."""
        url = f"{self.base_url}/values/{quote(key, safe='')}"
        resp = self.session.get(url)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        raise RuntimeError(f"KV GET {resp.status_code} for {key[:80]}")


# Global KV client (initialized in main)
kv_cache: KVClient | None = None
kv_scrape: KVClient | None = None


def init_kv_clients():
    global kv_cache, kv_scrape
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cache_ns = os.environ.get("CACHE_KV_NAMESPACE_ID")
    scrape_ns = os.environ.get("SCRAPE_DATA_KV_NAMESPACE_ID")

    if not all([account_id, api_token, cache_ns, scrape_ns]):
        print("Missing Cloudflare KV credentials", file=sys.stderr)
        return False

    kv_cache = KVClient(account_id, api_token, cache_ns)
    kv_scrape = KVClient(account_id, api_token, scrape_ns)
    return True


CATEGORY_INDEX: dict = {}
KV_STATS = {"ok": 0, "failed": 0}


def kv_put_cache(key: str, value: dict, ttl: int = 3600):
    if kv_cache:
        kv_cache.put(key, value, ttl)


def kv_put_scrape(key: str, value: dict, ttl: int = 86400) -> bool:
    if not kv_scrape:
        KV_STATS["failed"] += 1
        return False
    if isinstance(value, dict) and value.get("error"):
        # a site hiccup must not wipe previously good data
        try:
            old = kv_scrape.get(key)
            if old and not old.get("error"):
                print(f"  keeping previous good data for {key[:80]} ({value.get('error')})")
                return True
        except Exception:
            pass
    else:
        ttl = max(ttl, 7 * 86400) if ttl >= 86400 else ttl   # skipped-identical writes don't refresh TTL
    ok = kv_scrape.put(key, value, ttl)
    KV_STATS["ok" if ok else "failed"] += 1
    return ok


def search_key(site: str, query: str) -> str:
    # Must match the Worker: `search:${site}:${query.trim().toLowerCase()}`
    return f"search:{site}:{query.strip().lower()}"


try:
    from translate_titles import translate_result as _translate_result
except Exception:
    _translate_result = None


def translate_result(r: dict) -> dict:
    """Same title translation the local Flask app applies to scraped pages."""
    if not _translate_result or not isinstance(r, dict):
        return r
    try:
        return _translate_result(r)
    except Exception:
        return r


# Sites handled by the extra (sourcetest/extras) scrapers instead of the generic one - same rules as app.py
_REMOVED = ("pornoklad", "tlenporno", "xfuntaxy", "epornhome")
_norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
TEST_SITES[:] = [x for x in TEST_SITES
                 if not any(k in _norm(x.get("name")) + _norm(x.get("id")) + _norm(x.get("feed")) for k in _REMOVED)]


def _reg_host(url):
    h = (urlparse(url if "://" in (url or "") else "https://" + (url or "")).hostname or "").lower()
    return ".".join(h.split(".")[-2:]) if h else ""


_PLUS_HOSTS = {_reg_host(x["feed"]) for x in TEST_SITES} | {"porno-666.me"}


def use_plus(url: str) -> bool:
    try:
        return _reg_host(url) in _PLUS_HOSTS or bool(is_smart(url))
    except Exception:
        return False


# every video link seen while scraping (metadata is fetched for these)
VIDEO_LINKS: list = []
_VL_SEEN: set = set()
_VL_LOCK = threading.Lock()


def remember_links(result):
    for it in (result or {}).get("items") or []:
        link = it.get("link")
        if not link or it.get("_cam") or it.get("_chan"):
            continue
        with _VL_LOCK:
            if link not in _VL_SEEN:
                _VL_SEEN.add(link)
                VIDEO_LINKS.append(link)


# Scrape functions that write to KV
def scrape_and_cache_url(url: str, page_num: int | None = None, fresh: bool = False):
    key = f"scrape:{url}"
    try:
        if use_plus(url):
            result = scrape_plus(url, max_items=80) or {}
            items = result.get("items") or []
            result.setdefault("page", url)
            result["items"] = items
            result["count"] = len(items)
            result["page_num"] = page_num if page_num is not None else result.get("page_num") or _current_page_number(url)
            if items and not result.get("next_page"):
                result["next_page"] = _guess_next_page(url, result["page_num"])
                result["next_is_guess"] = True
        else:
            result = scrape_page(url, max_items=80, page_num=page_num)

        result = translate_result(result)
        remember_links(result)
        if kv_put_scrape(key, result, ttl=86400):
            print(f"  Cached: {url} ({result.get('count', 0)} items)")
        return result
    except Exception as e:
        error_result = {"page": url, "items": [], "count": 0, "error": str(e), "next_page": None, "page_num": page_num or 1}
        kv_put_scrape(key, error_result, ttl=3600)
        print(f"  Error caching {url}: {e}")
        return error_result


def _plus_search(site: str, query: str) -> dict:
    try:
        p = urlparse(site if "://" in site else "https://" + site)
        origin = f"{p.scheme}://{p.netloc}/"
        r = search_site(site_from_url(origin, None), query) or {}
    except Exception as e:
        r = {"error": str(e), "items": []}
    items = r.get("items") or []
    r["site"] = site
    r["query"] = query
    r["items"] = items
    r["count"] = len(items)
    r.setdefault("page", r.get("search_url") or site)
    r["source"] = "site-search" if items else "none"
    if items and not r.get("next_page") and r.get("search_url"):
        r["next_page"] = _guess_next_page(r["search_url"], _current_page_number(r["search_url"]))
    return r


def _score(it: dict, query: str) -> int:
    s = 0
    if it.get("thumbnail"): s += 10
    if it.get("duration"): s += 4
    if it.get("views"): s += 2
    if it.get("rating"): s += 1
    t = (it.get("title") or "").lower()
    q = query.lower()
    if t.startswith(q): s += 8
    elif q in t: s += 4
    return s


def scrape_search_and_cache(site: str, query: str, max_items: int = 40):
    """Pre-scrape one (site, query) pair under the key the Worker looks up (ranked like the local app)."""
    key = search_key(site, query)
    try:
        if use_plus(site):
            results = [_plus_search(site, query)]
        else:
            results = search_many([site], query, max_items=max_items, verify=False)
        results = [translate_result(r) if isinstance(r, dict) else r for r in results]
        combined, seen = [], set()
        for r in results:
            site_name = r.get("site") or r.get("page") or site
            for it in (r.get("items") or []):
                k = (it.get("link") or "").split("?")[0]
                if not k or k in seen:
                    continue
                seen.add(k)
                combined.append({**it, "_site": site_name, "_score": _score(it, query)})
        combined.sort(key=lambda x: -x["_score"])
        payload = {"results": results, "query": query, "combined": combined, "count": len(combined)}
        if kv_put_scrape(key, payload, ttl=86400):
            print(f"  Cached search: {query!r} on {site} ({len(combined)} items)")
        return payload
    except Exception as e:
        kv_put_scrape(key, {"results": [], "query": query, "combined": [], "count": 0, "error": str(e)}, ttl=3600)
        print(f"  Search error {site} {query!r}: {e}")


def scrape_resolve_and_cache(url: str, full: bool = False):
    key = f"resolve-full:{url}" if full else f"resolve:{url}"
    try:
        if use_plus(url) or full:
            result = deep_resolve(url)
        else:
            result = {"video": None, "error": "Use full resolve for this site"}
        kv_put_scrape(key, result, ttl=86400)
        print(f"  Cached resolve: {url}")
        return result
    except Exception as e:
        error_result = {"video": None, "error": str(e)}
        kv_put_scrape(key, error_result, ttl=3600)
        return error_result


def scrape_metadata_and_cache(url: str):
    key = f"meta:{url}"
    if not fetch_metadata:
        return {"url": url, "groups": [], "error": "metadata module unavailable"}

    try:
        html, final = fetch_html(url, timeout=20.0)
        data = fetch_metadata(html, final)
        kv_put_scrape(key, data, ttl=86400)
        print(f"  Cached metadata: {url}")
        return data
    except Exception as e:
        error_data = {"url": url, "groups": [], "error": str(e)}
        kv_put_scrape(key, error_data, ttl=3600)
        return error_data


def _cam_thumb_origins() -> dict:
    """image url -> Referer origin the provider's CDN expects (what local fetch_thumb() uses)."""
    try:
        import livecams as lc
        out = {}
        for u, prov in list(lc._THUMB_SRC.items()):
            site = lc._ORIGIN_OF.get(prov) or lc._ROOM_URL.get(prov, lc.HOME_URL).split("{")[0]
            out[u] = "/".join(site.split("/")[:3]) + "/"
        return out
    except Exception as e:
        print(f"  thumb origins unavailable: {e}")
        return {}


def scrape_livecams_and_cache(url: str | None = None, force: bool = False):
    key = f"livecams:{url or 'default'}"
    if not fetch_livecams:
        return {"items": [], "count": 0, "error": "livecams module unavailable"}

    try:
        data = fetch_livecams(url, force=force)
        kv_put_scrape(key, data, ttl=1800)  # 30 min
        origins = _cam_thumb_origins()
        if origins:
            kv_put_scrape("cam-thumb-origins", {"origins": origins}, ttl=1800)
        print(f"  Cached livecams: {len(data.get('items', []))} items, {len(origins)} thumb origins")
        return data
    except Exception as e:
        error_data = {"items": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data, ttl=600)
        return error_data


def scrape_channels_and_cache(category: str | None = None, page: int = 1, force: bool = False):
    key = f"channels:{category or 'all'}:{page}"
    if not fetch_channels:
        return {"items": [], "count": 0, "error": "channels module unavailable"}

    try:
        data = fetch_channels(category, page, force=force)
        kv_put_scrape(key, data, ttl=1800)
        print(f"  Cached channels: {len(data.get('items', []))} items")
        return data
    except Exception as e:
        error_data = {"items": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data, ttl=600)
        return error_data


def scrape_categories_and_cache(url: str, mode: str = "categories", page_num: int | None = None):
    key = f"categories:{url}:{mode}:{page_num or 1}"
    try:
        if mode in ("models", "pornstars"):
            data = scrape_models(url, page_num=page_num)
        elif mode == "sites":
            data = scrape_studio_sections(url)
        elif mode == "tags":
            data = scrape_tags(url, kind="porntags")
        else:
            data = scrape_categories(url, page_num=page_num)
        data = translate_result(data)
        kv_put_scrape(key, data, ttl=86400)
        print(f"  Cached categories: {url} ({data.get('count', 0)} items)")
        return data
    except Exception as e:
        error_data = {"categories": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data, ttl=3600)
        return error_data


# Main scraping jobs
def job_catalog_urls():
    """Write static catalog URLs to KV."""
    catalog = {
        "categories": "https://www.superporn.com/categories",
        "pornstars": "https://www.freesexvideos.xxx/models/",
        "studios": "https://www.freesexvideos.xxx/sites/",
        "bdsmStudios": "https://www.bdsmhole.com/studios/",
        "liveCams": "https://www.lemoncams.com/",
    }
    kv_put_scrape("catalog:urls", catalog, ttl=86400 * 7)
    print("Cached catalog URLs")


def job_test_sites():
    """Write test sites list to KV."""
    sites = [{"id": x["id"], "name": x["name"], "feed": x["feed"]} for x in TEST_SITES]
    kv_put_scrape("test-sites", {"sites": sites}, ttl=86400 * 7)
    print(f"Cached {len(sites)} test sites")


def scrape_feed_chain(url: str, pages: int | None = None):
    """Scrape page 1 and follow next_page so infinite scroll keeps hitting the cache."""
    pages = pages or int(os.environ.get("FEED_PAGES", "8"))
    result = scrape_and_cache_url(url)
    seen = {url}
    for n in range(2, pages + 1):
        nxt = (result or {}).get("next_page")
        if not nxt or nxt in seen or (result or {}).get("error") or not (result or {}).get("items"):
            break
        seen.add(nxt)
        time.sleep(1)
        result = scrape_and_cache_url(nxt, page_num=n)


def job_popular_feeds():
    """Scrape each frontend source plus a few pages of 'next'."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(scrape_feed_chain, url) for url in FRONTEND_SOURCES]
        for f in as_completed(futures):
            f.result()


# (listing url, mode, pages to follow)
LISTINGS = [
    ("https://www.superporn.com/categories", "categories", 2),
    ("https://www.superporn.com/series",     "sites",      2),   # Porn Series
    ("https://www.freesexvideos.xxx/models/", "models",    3),   # Pornstars
    ("https://www.freesexvideos.xxx/sites/",  "sites",     3),   # Networks (General)
    ("https://www.bdsmhole.com/studios/",     "categories", 2),  # Networks (BDSM)
    ("https://www.bdsmhole.com/categories/",  "categories", 2),
    ("https://www.porner.xxx/categories/",    "tags",      1),   # Porn Tags
]
# (listing url, env var, default count) -> how many "See All"/model/category feeds to pre-scrape
FEED_GROUPS = [
    ("https://www.superporn.com/categories",  "MAX_CATEGORY_FEEDS", 8),
    ("https://www.bdsmhole.com/categories/",  "MAX_BDSM_CATEGORY_FEEDS", 8),
    ("https://www.freesexvideos.xxx/sites/",  "MAX_NETWORK_FEEDS", 12),
    ("https://www.bdsmhole.com/studios/",     "MAX_BDSM_NETWORK_FEEDS", 8),
    ("https://www.freesexvideos.xxx/models/", "MAX_PORNSTAR_FEEDS", 12),
    ("https://www.superporn.com/series",      "MAX_SERIES_FEEDS", 6),
]


def _items_of(data: dict) -> list:
    for k in ("sections", "categories", "models", "items"):
        if data.get(k):
            return data[k]
    return []


def cache_listing_chain(url: str, mode: str, pages: int) -> list[str]:
    """Cache a listing page and its next pages (each under its own URL, like the frontend asks).
    Returns every item link found, in order."""
    links, cur, seen = [], url, set()
    for _ in range(pages):
        if not cur or cur in seen:
            break
        seen.add(cur)
        data = scrape_categories_and_cache(cur, mode)
        if not data or data.get("error"):
            break
        links += [i.get("link") for i in _items_of(data) if i.get("link")]
        cur = data.get("next_page")
        time.sleep(1)
    return links


def job_category_pages():
    """Scrape category / series / models / studio / tag listings (with a few pages each)."""
    for url, mode, pages in LISTINGS:
        CATEGORY_INDEX[url] = cache_listing_chain(url, mode, pages)
        print(f"Listing {url} [{mode}]: {len(CATEGORY_INDEX[url])} links")
        time.sleep(1)


def job_category_feeds():
    """Pre-scrape the video feeds behind the listings, so 'See All', category and pornstar clicks
    (and their infinite scroll) hit the cache."""
    cat_pages = int(os.environ.get("CATEGORY_FEED_PAGES", "2"))
    done = set()
    for index_url, env_name, default in FEED_GROUPS:
        limit = int(os.environ.get(env_name, str(default)))
        links = [l for l in CATEGORY_INDEX.get(index_url, []) if l not in done][:limit]
        done.update(links)
        print(f"Feeds for {index_url}: {len(links)}")
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda l: scrape_feed_chain(l, cat_pages), links))


def job_search_queries():
    """Pre-scrape common queries, one KV entry per (site, query)."""
    queries = ["milf", "teen", "anal", "amateur", "lesbian", "mature", "big tits", "blowjob"]
    for query in queries:
        for site in FRONTEND_SOURCES:
            scrape_search_and_cache(site, query)
            time.sleep(1)


def job_livecams_channels():
    """Live cams + live TV channels (a few pages, so the frontend can keep scrolling)."""
    scrape_livecams_and_cache()
    for page in range(1, int(os.environ.get("CHANNEL_PAGES", "3")) + 1):
        data = scrape_channels_and_cache(None, page)
        if not (data or {}).get("items"):
            break
        time.sleep(1)


META_SHARDS = "0123456789abcdef"


def meta_shard(url: str) -> str:
    # MUST match the Worker: first hex char of SHA-1(url)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[0]


def _fetch_one_meta(url: str):
    try:
        p = urlparse(url)
        html, final = fetch_html(url, timeout=20.0, referer=f"{p.scheme}://{p.netloc}/")
        data = fetch_metadata(html, final)
    except Exception:
        return url, None
    if not data or data.get("error"):
        return url, None            # failures are retried next run, never stored
    return url, data


def job_metadata():
    """Fetch duration / date / views / rating / tags / stars for videos seen this run.
    Stored in 16 shard keys (meta-shard:<hex>) = at most 16 KV writes per run."""
    if not fetch_metadata:
        print("metadata module unavailable - skipping")
        return
    limit = int(os.environ.get("MAX_METADATA", "400"))
    per_shard = int(os.environ.get("META_PER_SHARD", "250"))
    max_age = 14 * 86400
    now = int(time.time())

    shards = {}
    for c in META_SHARDS:
        cur = kv_scrape.get(f"meta-shard:{c}")          # raises on outage -> nothing gets overwritten
        shards[c] = dict(cur.get("items", {})) if isinstance(cur, dict) else {}

    todo = [l for l in VIDEO_LINKS if l not in shards[meta_shard(l)]][:limit]
    print(f"Metadata: {len(VIDEO_LINKS)} videos seen, {len(todo)} to fetch")
    dirty = set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for url, data in pool.map(_fetch_one_meta, todo):
            if data:
                data["_ts"] = now
                shards[meta_shard(url)][url] = data
                dirty.add(meta_shard(url))

    for c, items in shards.items():
        fresh = {u: m for u, m in items.items() if now - int(m.get("_ts", now)) < max_age}
        if len(fresh) > per_shard:
            newest = sorted(fresh.items(), key=lambda kv: -int(kv[1].get("_ts", 0)))[:per_shard]
            fresh = dict(newest)
        if len(fresh) != len(items):
            dirty.add(c)
        shards[c] = fresh
    for c in sorted(dirty):
        kv_put_scrape(f"meta-shard:{c}", {"items": shards[c]}, ttl=30 * 86400)
    print(f"Metadata: wrote {len(dirty)} shard(s), {sum(len(v) for v in shards.values())} videos stored")


def main():
    if not init_kv_clients():
        sys.exit(1)

    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    print(f"Starting Cloudflare KV scraper{' (' + only + ' only)' if only else ''}...")
    start = time.time()

    if only == "live":
        job_livecams_channels()
    else:
        # Static data
        job_catalog_urls()
        job_test_sites()

        # Dynamic scraping
        job_popular_feeds()
        job_category_pages()
        job_category_feeds()
        job_search_queries()
        job_livecams_channels()
        job_metadata()

    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.1f}s - KV writes ok={KV_STATS['ok']} failed={KV_STATS['failed']}")
    if KV_STATS["failed"] and KV_STATS["failed"] >= KV_STATS["ok"]:
        print("Too many KV write failures", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
