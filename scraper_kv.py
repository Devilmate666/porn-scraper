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
import functools
import traceback
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
    resolve_full_video_url,
)
# Frontend SOURCES (from index.html) - these are what the frontend expects
FRONTEND_SOURCES = [
    "https://www.superporn.com/",
    "https://2023.pornvideobb.com/",
    "https://www.freesexvideos.xxx/",
    "https://www.bdsmhole.com/",
]

from searchkit import (parse_query, query_key, rank_combined, index_record, reg_host, norm as _sk_norm, stems as _sk_stems)
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

    def _request(self, method: str, url: str, **kw):
        """HTTP with retry/backoff on 429 / 5xx / network errors (Cloudflare's API rate-limits bursts)."""
        last = None
        for attempt in range(5):
            try:
                resp = self.session.request(method, url, timeout=30, **kw)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    return resp
                last = RuntimeError(f"HTTP {resp.status_code}")
                wait = float(resp.headers.get("Retry-After") or 0) or (1.5 * (2 ** attempt))
            except requests.RequestException as e:
                last, wait = e, 1.5 * (2 ** attempt)
            time.sleep(min(wait, 30))
        raise last or RuntimeError("request failed")

    def load_expirations(self):
        """Read every key's expiry once (list API, 1 request / 1000 keys). Lets skip_same refuse to 'skip' a write
        for a key that is about to expire - the cause of data vanishing after a perfectly good scrape."""
        exp, cursor = {}, None
        try:
            while True:
                params = {"limit": 1000, **({"cursor": cursor} if cursor else {})}
                r = self._request("GET", f"{self.base_url}/keys", params=params)
                if r.status_code != 200:
                    raise RuntimeError(f"list {r.status_code}")
                j = r.json()
                for k in j.get("result", []):
                    exp[k["name"]] = k.get("expiration")          # None = never expires
                cursor = (j.get("result_info") or {}).get("cursor")
                if not cursor:
                    break
            self.exp = exp
            print(f"  KV: {len(exp)} existing keys indexed")
        except Exception as e:
            self.exp = None
            print(f"  KV key listing failed ({e}) - will rewrite instead of skipping identical values")

    def put(self, key: str, value: dict, expiration_ttl: int = 3600, skip_same: bool = True) -> bool:
        """Write to KV with TTL in seconds. Key MUST be URL-encoded (it contains '/' and ':').
        Identical values are only skipped when the existing entry still has > 14 days to live."""
        if skip_same and getattr(self, "exp", None) is not None and key in self.exp:
            left = (self.exp[key] - time.time()) if self.exp[key] else 1e12
            if left > 14 * 86400:
                try:
                    if self.get(key) == value:
                        return True
                except Exception:
                    pass
        url = f"{self.base_url}/values/{quote(key, safe='')}"
        params = {"expiration_ttl": max(expiration_ttl, 60)} if expiration_ttl else {}
        try:
            resp = self._request("PUT", url, data=json.dumps(value), params=params)
        except Exception as e:
            print(f"  KV PUT ERROR key={key[:90]} {e}", file=sys.stderr)
            return False
        if resp.status_code not in (200, 201):
            print(f"  KV PUT FAILED {resp.status_code} key={key[:90]} body={resp.text[:200]}", file=sys.stderr)
            return False
        if getattr(self, "exp", None) is not None:
            self.exp[key] = time.time() + max(expiration_ttl, 60)
        return True

    def put_batch(self, entries: list[tuple[str, dict]], expiration_ttl: int = 3600) -> int:
        success = 0
        for key, value in entries:
            if self.put(key, value, expiration_ttl):
                success += 1
            time.sleep(0.2)  # 5 writes/sec to avoid 429 burst limit
        return success

    def get(self, key: str) -> dict | None:
        """None = key does not exist. Raises on any other failure (so callers never mistake an outage for 'empty')."""
        url = f"{self.base_url}/values/{quote(key, safe='')}"
        resp = self._request("GET", url)
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


KV_LONG = 30 * 86400                                   # good data lives 30 days: the Worker serves stale data, never a hole
LIVE_PREFIXES = ("livecams:", "channels-bundle:", "cam-thumb-origins", "catalog:", "search-index", "taxonomy-index")   # stamped with _ts
PRIORITY_PREFIXES = LIVE_PREFIXES + ("scrape-status",)                                             # never skipped by the write budget
MAX_WRITES = int(os.environ.get("MAX_KV_WRITES", "450"))                                            # per run (free tier: 1,000/day)
KV_STATS.setdefault("skipped", 0)


def _has_data(v) -> bool:
    """True when a payload actually carries something worth serving."""
    if not isinstance(v, dict):
        return bool(v)
    known = False
    for k in ("items", "categories", "sections", "models", "combined", "pages", "sites", "origins", "records", "entries"):
        if k in v:
            if v[k]:
                return True
            known = True
    if "results" in v:
        if any(isinstance(r, dict) and (r.get("items") or r.get("categories")) for r in (v["results"] or [])):
            return True
        known = True
    if "video" in v or "all" in v:
        return bool(v.get("video") or v.get("all"))
    if "groups" in v:
        return bool(v["groups"]) or bool(v.get("title"))
    return not known                                   # unknown shape (e.g. the catalog dict) counts as data


def kv_put_scrape(key: str, value: dict, ttl: int = 86400) -> bool:
    if not kv_scrape:
        KV_STATS["failed"] += 1
        return False
    priority = key.startswith(PRIORITY_PREFIXES)
    is_err = isinstance(value, dict) and bool(value.get("error"))
    empty = isinstance(value, dict) and not is_err and not _has_data(value)
    if is_err or empty:
        # a site hiccup / blocked IP / empty result must never wipe previously good data
        try:
            old = kv_scrape.get(key)
        except Exception as e:
            print(f"  KV unreadable ({e}) - not overwriting {key[:80]}")
            return False
        if old and not old.get("error") and _has_data(old):
            print(f"  keeping previous good data for {key[:80]} ({value.get('error') or 'empty result'})")
            return True
        ttl = 3600 if empty else min(ttl, 900)         # nothing to protect: remember the failure only briefly
    else:
        ttl = KV_LONG
        if key.startswith(LIVE_PREFIXES) and isinstance(value, dict):
            value = {**value, "_ts": int(time.time())}  # also makes the value differ, so the TTL is always refreshed
    if not priority and KV_STATS["ok"] >= MAX_WRITES:
        KV_STATS["skipped"] += 1
        return False
    ok = kv_scrape.put(key, value, ttl)
    KV_STATS["ok" if ok else "failed"] += 1
    return ok


# ---------------------------------------------------------------- resilience helpers
JOB_STATUS: dict = {}
DEADLINE = time.time() + int(os.environ.get("SCRAPE_BUDGET_SECONDS", str(33 * 60)))


def time_left() -> float:
    return DEADLINE - time.time()


def deadline_guard(fn):
    """Skip work that would start after the run's time budget (the job is killed at 40 min; this exits cleanly first)."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if time_left() < 30:
            return {"items": [], "count": 0, "error": "time budget reached"}
        return fn(*a, **k)
    return wrapper


def retry_until(fn, ok=lambda r: True, tries: int = 3, delay: float = 4.0, what: str = "op"):
    """Call fn() until ok(result) (or no exception); returns the last result. Exceptions are retried too."""
    last, exc = None, None
    for n in range(tries):
        try:
            last, exc = fn(), None
            if ok(last):
                return last
        except Exception as e:
            exc = e
        if n < tries - 1:
            print(f"  {what}: attempt {n + 1}/{tries} failed ({exc or (last or {}).get('error')}), retrying")
            time.sleep(delay * (n + 1))
    if exc and last is None:
        raise exc
    return last


def safe_job(name: str, fn, *a, **k):
    """One failing job never stops the others."""
    if time_left() < 45:
        print(f"[{name}] skipped: time budget used up")
        JOB_STATUS[name] = {"ok": False, "skipped": "time budget"}
        return None
    t0 = time.time()
    try:
        r = fn(*a, **k)
        JOB_STATUS.setdefault(name, {"ok": True})
        JOB_STATUS[name]["s"] = round(time.time() - t0, 1)
        return r
    except Exception as e:
        traceback.print_exc()
        JOB_STATUS[name] = {"ok": False, "s": round(time.time() - t0, 1), "error": str(e)[:200]}
        return None


def search_key(site: str, query: str) -> str:
    # Must match the Worker: `search:${site}:${queryKey(query)}` (trim, lower-case, collapse whitespace)
    return f"search:{site}:{query_key(query)}"


# every video link seen while scraping (metadata is fetched for these)
VIDEO_LINKS: list = []
_VL_SEEN: set = set()
_VL_LOCK = threading.Lock()
VIDEO_ITEMS: dict = {}            # link -> the card (title, thumbnail, duration ...) = the raw material of the search index
LINK_VIA: dict = {}               # link -> labels it was found under: the category/tag feed it came from, the keyword that found it
PRIORITY_META: list = []          # video pages visitors opened that had no metadata yet (the Worker queues {t: "meta"})


def remember_links(result, via=None):
    for it in (result or {}).get("items") or []:
        link = it.get("link")
        if not link or it.get("_cam") or it.get("_chan"):
            continue
        with _VL_LOCK:
            if link not in _VL_SEEN:
                _VL_SEEN.add(link)
                VIDEO_LINKS.append(link)
            VIDEO_ITEMS.setdefault(link, it)
            if via:
                tags = LINK_VIA.setdefault(link, [])
                for v in ([via] if isinstance(via, str) else via):
                    if v and v not in tags and len(tags) < 6:
                        tags.append(v)


# Scrape functions that write to KV
@deadline_guard
def scrape_and_cache_url(url: str, page_num: int | None = None, fresh: bool = False, via=None):
    key = f"scrape:{url}"
    try:
        result = scrape_page(url, max_items=80, page_num=page_num)

        remember_links(result, via)
        if kv_put_scrape(key, result, ttl=86400):
            print(f"  Cached: {url} ({result.get('count', 0)} items)")
        return result
    except Exception as e:
        error_result = {"page": url, "items": [], "count": 0, "error": str(e), "next_page": None, "page_num": page_num or 1}
        kv_put_scrape(key, error_result, ttl=3600)
        print(f"  Error caching {url}: {e}")
        return error_result


TAXONOMY: dict = {}               # page url -> {"n": name, "u": url, "k": category|tag|model|studio, "h": registrable host}
CATEGORY_NAMES: dict = {}         # feed url -> its category / star / studio name (labels the videos scraped from that feed)
_KIND_OF_MODE = {"categories": "category", "tags": "tag", "models": "model", "pornstars": "model", "sites": "studio"}


def register_taxonomy(items, mode):
    kind = _KIND_OF_MODE.get(mode, "category")
    for i in items or []:
        link, name = i.get("link"), (i.get("name") or i.get("title") or "").strip()
        if link and name and len(name) <= 60 and link not in TAXONOMY:
            TAXONOMY[link] = {"n": name, "u": link, "k": kind, "h": reg_host(link)}
        if link and name:
            CATEGORY_NAMES.setdefault(link.split("?")[0], name)


def load_taxonomy_from_kv():
    """Live / wanted runs do not scrape the listings: start from what the last full run stored."""
    try:
        cur = kv_scrape.get("taxonomy-index") if kv_scrape else None
    except Exception as e:
        print(f"  taxonomy-index unreadable ({e})")
        return
    for e in (cur or {}).get("entries") or []:
        if e.get("u"):
            TAXONOMY.setdefault(e["u"], e)
    print(f"  taxonomy: {len(TAXONOMY)} pages known")


def taxonomy_for(site: str, q: dict, limit: int = 2) -> list:
    """Category / tag / star / studio pages of `site` whose name equals / contains / is contained in the keyword.
    Same rules as matchTaxonomy() in src/search.ts."""
    host, out = reg_host(site), []
    for e in TAXONOMY.values():
        if e.get("h") != host:
            continue
        ns = _sk_stems(e["n"])
        if not ns:
            continue
        if ns == q["stems"]:
            sc = 3
        elif all(x in ns for x in q["stems"]):
            sc = 2
        elif all(x in q["stems"] for x in ns) and len("".join(ns)) >= 4:
            sc = 1
        else:
            continue
        out.append((sc + (0.2 if e.get("k") in ("category", "tag") else 0), e))
    out.sort(key=lambda t: -t[0])
    return [e for _, e in out[:limit]]


@deadline_guard
def scrape_chain_from(url: str, first_num: int = 1, pages: int = 3, via=None):
    """Cache `url` (page `first_num`) and follow next_page for `pages` pages in total. Each page is stored
    under scrape:{its own url}, which is exactly what the frontend asks for while scrolling."""
    cur, n, seen = url, first_num, set()
    for _ in range(max(1, pages)):
        if not cur or cur in seen:
            break
        seen.add(cur)
        result = scrape_and_cache_url(cur, page_num=n, via=via)
        if not result or result.get("error") or not result.get("items"):
            break
        cur, n = result.get("next_page"), n + 1
        time.sleep(1)


@deadline_guard
def scrape_search_and_cache(site: str, query: str, max_items: int = 40, pages: int | None = None):
    """Pre-scrape one (site, keyword) pair under the key the Worker looks up.
    "Search everything": the site's own search page PLUS the category / tag / star / studio page that carries the keyword's
    name, ranked together on title, tags, genres, stars, studios and description (searchkit.rank_combined)."""
    query = query_key(query)
    key = search_key(site, query)
    q = parse_query(query)
    try:
        results = search_many([site], query, max_items=max_items, verify=False)
        for r in results:
            remember_links(r, q["norm"])                   # the site's search matched it: the keyword becomes an index term for the video
        for e in taxonomy_for(site, q):
            page = None
            try:
                page = kv_scrape.get(f"scrape:{e['u']}") if kv_scrape else None   # usually pre-scraped by the category-feed job
            except Exception:
                pass
            if not (page and page.get("items")):
                page = scrape_and_cache_url(e["u"], via=e["n"])
            if page and page.get("items") and not page.get("error"):
                remember_links(page, e["n"])
                results.append({**page, "items": [{**it, "_via": e["n"]} for it in page["items"]], "site": site, "query": query,
                                "source": "taxonomy", "via": e["n"], "search_url": e["u"]})
                print(f"    + {e['k']} page {e['n']!r}: {len(page['items'])} items")
        combined = rank_combined([r for r in results if isinstance(r, dict)], q)
        payload = {"results": results, "query": query, "combined": combined, "count": len(combined)}
        if kv_put_scrape(key, payload, ttl=86400):
            print(f"  Cached search: {query!r} on {site} ({len(combined)} items)")
        # infinite scroll on a search = the following result pages, fetched through /api/scrape
        pages = pages if pages is not None else int(os.environ.get("SEARCH_PAGES", "3"))
        first = next((r for r in results if isinstance(r, dict) and r.get("source") != "taxonomy"), {}) or {}
        if pages > 1 and combined and first.get("next_page"):
            scrape_chain_from(first["next_page"], int(first.get("page_num") or 1) + 1, pages - 1, via=q["norm"])
        return payload
    except Exception as e:
        kv_put_scrape(key, {"results": [], "query": query, "combined": [], "count": 0, "error": str(e)}, ttl=3600)
        print(f"  Search error {site} {query!r}: {e}")


@deadline_guard
def scrape_resolve_and_cache(url: str, full: bool = False):
    key = f"resolve-full:{url}" if full else f"resolve:{url}"
    try:
        if full:
            result = resolve_full_video_url(url, fresh=True)
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


def _kv_doc(key: str):
    """(document|None, age in seconds|None). Never raises."""
    try:
        d = kv_scrape.get(key) if kv_scrape else None
    except Exception:
        return None, None
    ts = (d or {}).get("_ts")
    return d, (time.time() - ts) if ts else None


def _carry_cams(old: dict | None, new: dict) -> dict:
    """GitHub's IPs are refused by some platforms, so a run here often sees fewer platforms than the Worker did.
    Keep each missing platform's cams from the stored copy (only cams actually seen in the last 3 h)."""
    if not old or not old.get("items"):
        return new
    got = {c.get("provider") for c in new.get("items", [])}
    cutoff = time.time() - 3 * 3600
    extra = [c for c in old["items"] if c.get("provider") and c["provider"] not in got and (c.get("_seen") or 0) >= cutoff]
    if extra:
        new = {**new, "items": (new.get("items", []) + extra)[:500], "carried_over": sorted({c.get("provider_name") or c["provider"] for c in extra})}
        new["count"] = len(new["items"])
    return new


def scrape_livecams_and_cache(url: str | None = None, force: bool = False):
    key = f"livecams:{url or 'default'}"
    if not fetch_livecams:
        return {"items": [], "count": 0, "error": "livecams module unavailable"}
    old, age = _kv_doc(key)
    # the Worker refreshes cams itself every 10 minutes: a fresh copy is not rewritten (saves KV writes and the retries below)
    if not url and not force and old and old.get("items") and age is not None and age < int(os.environ.get("CAMS_MIN_AGE", "600")):
        print(f"  livecams: stored copy is {int(age)}s old (kept by the Worker) - skipped")
        return old
    try:
        # platforms sometimes refuse a GitHub IP for a minute: retry before giving up
        data = retry_until(lambda: fetch_livecams(url, force=True), ok=lambda r: bool((r or {}).get("items")),
                           tries=3, delay=6, what="livecams")
        for note in (data or {}).get("diagnostics", [])[:12]:
            print(f"    cams: {note}")
        if not url:
            data = _carry_cams(old, data)
        kv_put_scrape(key, data)                      # empty/error results keep the previous good copy
        origins = _cam_thumb_origins()
        if origins:
            kv_put_scrape("cam-thumb-origins", {"origins": origins})
        print(f"  Cached livecams: {len(data.get('items', []))} items, {len(origins)} thumb origins")
        return data
    except Exception as e:
        error_data = {"items": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data)
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


def scrape_channels_bundle(category: str | None = None, max_pages: int | None = None, force: bool = False):
    """Every channel page in ONE KV key (channels-bundle:{category|all}) = 1 write however deep the scroll goes.
    The Worker slices the page the frontend asks for; pages past the end come back as 'end of list'."""
    cat = category or "all"
    max_pages = max_pages or int(os.environ.get("CHANNEL_PAGES", "20"))
    key = f"channels-bundle:{cat}"
    old, age = _kv_doc(key)
    if not force and old and old.get("pages") and age is not None and age < int(os.environ.get("CHANNELS_MIN_AGE", "3000")):
        print(f"  channels[{cat}]: stored copy is {int(age)}s old - skipped")
        return old
    if not fetch_channels:
        return {"pages": {}, "error": "channels module unavailable"}
    pages, err = {}, None
    for n in range(1, max_pages + 1):
        try:
            if n == 1:
                data = retry_until(lambda: fetch_channels(category, 1, force=True),
                                   ok=lambda r: bool((r or {}).get("items")), tries=3, delay=6, what="channels")
            else:
                data = fetch_channels(category, n)
        except Exception as e:
            err = str(e)
            print(f"  channels[{cat}] page {n} raised: {err[:200]}")
            break
        if not data or data.get("error") or not data.get("items"):
            err = (data or {}).get("error") if n == 1 else None
            if n == 1:
                print(f"  channels[{cat}] page 1 failed: {err or 'page loaded but no channel cards found'}")
                for note in ((data or {}).get("diagnostics") or [])[:8]:
                    print(f"    channels: {note}")
            break
        pages[str(n)] = data
        if not data.get("next_page"):
            break
        time.sleep(0.5)
    if pages:
        bundle = {"pages": pages}
        kv_put_scrape(key, bundle, ttl=1800)
        print(f"  Cached channels[{cat}]: {len(pages)} page(s), {sum(len(p.get('items', [])) for p in pages.values())} items")
        return bundle
    bundle = {"pages": {}, "error": err or "no channels"}
    kv_put_scrape(key, bundle, ttl=600)          # keeps previous good data if there is any
    return bundle


@deadline_guard
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


def scrape_feed_chain(url: str, pages: int | None = None, via=None):
    """Scrape page 1 and follow next_page so infinite scroll keeps hitting the cache."""
    pages = pages or int(os.environ.get("FEED_PAGES", "8"))
    result = scrape_and_cache_url(url, via=via)
    seen = {url}
    for n in range(2, pages + 1):
        nxt = (result or {}).get("next_page")
        if not nxt or nxt in seen or (result or {}).get("error") or not (result or {}).get("items"):
            break
        seen.add(nxt)
        time.sleep(1)
        result = scrape_and_cache_url(nxt, page_num=n, via=via)


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
        register_taxonomy(_items_of(data), mode)
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
            list(ex.map(lambda l: scrape_feed_chain(l, cat_pages, via=CATEGORY_NAMES.get((l or "").split("?")[0])), links))


def job_search_queries():
    """Pre-scrape common queries, one KV entry per (site, query)."""
    queries = ["milf", "teen", "anal", "amateur", "lesbian", "mature", "big tits", "blowjob"]
    for query in queries:
        for site in FRONTEND_SOURCES:
            scrape_search_and_cache(site, query)
            time.sleep(1)


def job_livecams_channels(with_categories: bool = False):
    """Live cams + ALL live-TV channel pages in one bundle key. 3 KV writes per run (livecams, thumb origins, channels).
    Category chips are refreshed on full runs."""
    cams = scrape_livecams_and_cache() or {}
    JOB_STATUS["livecams"] = {"ok": bool(cams.get("items")), "count": len(cams.get("items") or []), "error": cams.get("error")}
    bundle = scrape_channels_bundle(None) or {}
    pages = bundle.get("pages") or {}
    JOB_STATUS["channels"] = {"ok": bool(pages), "pages": len(pages), "error": bundle.get("error")}
    if with_categories and pages:
        first = pages.get("1") or {}
        slugs = [c.get("slug") for c in (first.get("categories") or []) if c.get("slug")]
        for slug in slugs[: int(os.environ.get("CHANNEL_CATEGORIES", "6"))]:
            scrape_channels_bundle(slug)
            time.sleep(1)


def _wanted_id(it: dict) -> str:
    return json.dumps(it, sort_keys=False)


def _run_wanted(it: dict):
    t = it.get("t")
    try:
        if t == "search" and it.get("site") and it.get("query"):
            scrape_search_and_cache(it["site"], it["query"], pages=int(os.environ.get("WANTED_SEARCH_PAGES", "5")))
        elif t == "scrape" and it.get("url"):
            scrape_chain_from(it["url"], int(it.get("page_num") or 1), int(os.environ.get("WANTED_FEED_PAGES", "3")))
        elif t == "resolve" and it.get("url"):
            scrape_resolve_and_cache(it["url"], full=bool(it.get("full")))
        elif t == "channels":
            scrape_channels_bundle(it.get("category"))
        elif t == "categories" and it.get("url"):
            scrape_categories_and_cache(it["url"], it.get("mode") or "categories", it.get("page_num"))
        elif t == "meta" and it.get("url"):
            if it["url"] not in PRIORITY_META:
                PRIORITY_META.append(it["url"])            # a visitor opened this video and it had no metadata: fetch it first
    except Exception as e:
        print(f"  wanted {t} failed: {e}")


def job_wanted():
    """Fetch what visitors asked for that the cache did not have (the Worker queues it in CACHE: wanted:queue)."""
    if not kv_cache:
        return
    try:
        q = kv_cache.get("wanted:queue")
    except Exception as e:
        print(f"wanted queue unreadable: {e}")
        return
    items = (q or {}).get("items") or []
    if not items:
        print("Wanted queue: empty")
        return
    batch = items[: int(os.environ.get("MAX_WANTED", "30"))]
    print(f"Wanted queue: {len(items)} queued, processing {len(batch)}")
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(_run_wanted, batch))
    done = {_wanted_id(i) for i in batch}
    try:
        cur = kv_cache.get("wanted:queue") or {}          # re-read: the Worker may have queued more meanwhile
        cur["items"] = [i for i in (cur.get("items") or []) if _wanted_id(i) not in done]
        kv_cache.put("wanted:queue", cur, 3 * 86400, skip_same=False)
    except Exception as e:
        print(f"could not update wanted queue: {e}")


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


META_ALL: dict = {}               # video url -> metadata (every shard after job_metadata ran): feeds the search index


def job_metadata(limit: int | None = None):
    """Fetch duration / date / views / rating / tags / genres / stars / studios for videos seen this run, and FIRST for the
    ones visitors opened that had none (PRIORITY_META). Stored in 16 shard keys (meta-shard:<hex>) = at most 16 KV writes."""
    if not fetch_metadata:
        print("metadata module unavailable - skipping")
        return
    limit = limit or int(os.environ.get("MAX_METADATA", "400"))
    per_shard = int(os.environ.get("META_PER_SHARD", "250"))
    max_age = 14 * 86400
    now = int(time.time())

    shards = {}
    for c in META_SHARDS:
        cur = kv_scrape.get(f"meta-shard:{c}")          # raises on outage -> nothing gets overwritten
        shards[c] = dict(cur.get("items", {})) if isinstance(cur, dict) else {}

    queue = list(dict.fromkeys(PRIORITY_META + VIDEO_LINKS))
    todo = [l for l in queue if l not in shards[meta_shard(l)]][:limit]
    print(f"Metadata: {len(VIDEO_LINKS)} videos seen, {len(PRIORITY_META)} requested by visitors, {len(todo)} to fetch")
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
    for items in shards.values():
        META_ALL.update(items)
    print(f"Metadata: wrote {len(dirty)} shard(s), {sum(len(v) for v in shards.values())} videos stored")


def job_taxonomy_index():
    """ONE key with every category / tag / star / studio page the scraper has seen. The Worker opens the page whose name
    matches the typed keyword, so a keyword that is a genre or a star finds videos whose title never says it."""
    cap = int(os.environ.get("TAXONOMY_MAX", "4000"))
    entries = list(TAXONOMY.values())[:cap]
    if entries:
        kv_put_scrape("taxonomy-index", {"entries": entries})
        print(f"Taxonomy index: {len(entries)} pages")


def job_search_index():
    """ONE key (`search-index`) = every video the scraper has seen, with the tags / genres / stars / studios from its
    metadata and the category feed or keyword it was found under. The Worker searches it with zero network cost.
    Accumulates across runs (new data wins, fields the new run lacks are kept), expires after 21 days, capped."""
    if not kv_scrape:
        return
    cap = int(os.environ.get("INDEX_MAX", "2500"))
    max_age, now = 21 * 86400, int(time.time())
    try:
        cur = kv_scrape.get("search-index")                  # raises on outage -> never overwrite on a failed read
    except Exception as e:
        print(f"search-index unreadable ({e}) - not rebuilding")
        return
    old = {r["l"]: r for r in (cur or {}).get("records") or [] if r.get("l")}
    records = {}
    for link in set(old) | set(VIDEO_ITEMS):
        prev, item = old.get(link), VIDEO_ITEMS.get(link)
        if item is None:                                      # not seen this run: rebuild the card from the stored record
            item = {"link": link, "title": prev.get("t"), "thumbnail": prev.get("i"), "duration": prev.get("du"), "views": prev.get("vw"),
                    "rating": prev.get("rt"), "added": prev.get("ad"), "quality": prev.get("ql"), "page": prev.get("p")}
        via = list(dict.fromkeys(LINK_VIA.get(link, []) + ((prev or {}).get("vi") or [])))
        rec = index_record(item, META_ALL.get(link), via=via, ts=now if link in VIDEO_ITEMS else (prev or {}).get("ts", now))
        for k in ("tg", "ct", "md", "st", "ds"):              # keep what an earlier run learnt when this one has nothing
            if k not in rec and prev and prev.get(k):
                rec[k] = prev[k]
        if rec.get("t") and now - int(rec.get("ts", now)) < max_age:
            records[link] = rec
    ordered = sorted(records.values(), key=lambda r: -int(r.get("ts", 0)))[:cap]
    if not ordered:
        print("Search index: nothing to write")
        return
    kv_put_scrape("search-index", {"records": ordered})
    tagged = sum(1 for r in ordered if r.get("tg") or r.get("ct") or r.get("md") or r.get("st") or r.get("vi"))
    print(f"Search index: {len(ordered)} videos ({tagged} with tags/genres/stars/studios)")


def write_status(only, started):
    """One small record the Worker reads for /healthz and its watchdog."""
    if not kv_cache:
        return
    try:
        kv_cache.put("scrape-status", {"ts": int(time.time()), "mode": only or "full", "elapsed": round(time.time() - started, 1),
                                       "jobs": JOB_STATUS, "kv": KV_STATS}, KV_LONG, skip_same=False)
    except Exception as e:
        print(f"could not write scrape-status: {e}")


def main():
    if not init_kv_clients():
        sys.exit(1)

    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    print(f"Starting Cloudflare KV scraper{' (' + only + ' only)' if only else ''}...")
    start = time.time()
    kv_scrape.load_expirations()

    load_taxonomy_from_kv()
    if only in ("live", "wanted"):
        if only == "live":
            safe_job("live", job_livecams_channels)
        safe_job("wanted", job_wanted)
        if PRIORITY_META or VIDEO_ITEMS:                  # what visitors asked for goes into the metadata shards + search index right away
            safe_job("metadata", job_metadata, limit=80)
            safe_job("index", job_search_index)
    else:
        # perishable data first, then what visitors asked for, then the long tail - each job isolated
        safe_job("catalog", job_catalog_urls)
        safe_job("live", job_livecams_channels, with_categories=True)
        safe_job("wanted", job_wanted)
        safe_job("feeds", job_popular_feeds)
        safe_job("listings", job_category_pages)
        safe_job("category_feeds", job_category_feeds)
        safe_job("search", job_search_queries)
        safe_job("metadata", job_metadata)
        safe_job("taxonomy", job_taxonomy_index)
        safe_job("index", job_search_index)

    elapsed = time.time() - start
    write_status(only, start)
    print(f"\nCompleted in {elapsed:.1f}s - KV writes ok={KV_STATS['ok']} failed={KV_STATS['failed']} skipped={KV_STATS['skipped']}")
    for name, st in JOB_STATUS.items():
        print(f"  {'OK ' if st.get('ok') else 'FAIL'} {name}: {st}")
    if KV_STATS["failed"] and KV_STATS["failed"] >= KV_STATS["ok"]:
        print("Too many KV write failures", file=sys.stderr)
        sys.exit(1)
    # a live run where BOTH perishable sources failed should be red in GitHub (you get notified); data stays served from KV
    if only == "live" and not JOB_STATUS.get("livecams", {}).get("ok") and not JOB_STATUS.get("channels", {}).get("ok"):
        print("Both livecams and channels failed this run (previous data is still being served)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
