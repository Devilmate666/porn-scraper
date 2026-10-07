"""Flask backend: JSON scraping API (no media proxy: the browser talks to video hosts directly).

Routes (match what index.html calls):
  POST /api/scrape             {urls:[str|{url,page_num}]}
  POST /api/search             {sites:[...], query}
  POST /api/resolve            {url}
  POST /api/resolve-full       {url}
  POST /api/scrape-categories  {url, mode?: 'sites'|'tags'}
  POST /api/livecams           {url?, force?}
  GET  /healthz
"""
import ipaddress
import json
import os
import re
import socket
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, quote

from searchkit import parse_query, rank_combined
from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS

from scraper import (
    scrape_many, resolve_video_url, resolve_full_video_url, search_many,
    scrape_categories, scrape_tags, scrape_studio_sections,
    scrape_superporn_categories, scrape_models,
)
from sourcetest import TEST_SITES, test_site, search_site, site_from_url
from scraper import scrape_page, _guess_next_page, _current_page_number, fetch_html
try:
    from metadata import fetch_metadata
except Exception as _e:          # metadata is a bonus: never take the whole app down
    print(f"[metadata] disabled: {_e}", flush=True)
    def fetch_metadata(html, url):
        return {"url": url, "groups": [], "error": "metadata module unavailable"}
from extras import scrape_plus, deep_resolve, is_smart
try:
    from livecams import fetch_livecams, fetch_thumb
except Exception as _e:          # the cams tab is a bonus: never take the whole app down
    print(f"[livecams] disabled: {_e}", flush=True)
    def fetch_livecams(url=None, force=False):
        return {"items": [], "count": 0, "error": "livecams module unavailable", "diagnostics": [str(_e)]}

    def fetch_thumb(url):
        raise RuntimeError("livecams module unavailable")
try:
    from channels import fetch_channels
except Exception as _e:          # the channels tab is a bonus: never take the whole app down
    print(f"[channels] disabled: {_e}", flush=True)
    def fetch_channels(category=None, page=1, force=False):
        return {"items": [], "count": 0, "error": "channels module unavailable", "diagnostics": [str(_e)]}
try:
    from translate_titles import translate_result, translate_items, host_needs_translate
except Exception:
    def translate_result(r): return r
    def translate_items(items, page_url=None): return items or []
    def host_needs_translate(url): return False

app = Flask(__name__)

# ---------------------------------------------------------------- test list -> real sources
def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


# Sites that graduated from the Test list. Their working feed URL is read from the test list
# itself and injected into the page, so the frontend does not need to hard-code it.
_PROMOTE = {"porno666": "Porno-666"}
PROMOTED_SOURCES = []

# Removed sources (never listed, promoted or routed to the extra scrapers)
_REMOVED = ("pornoklad", "tlenporno", "xfuntaxy")
TEST_SITES[:] = [x for x in TEST_SITES
                 if not any(k in _norm(x.get("name")) + _norm(x.get("id")) + _norm(x.get("feed"))
                            for k in _REMOVED)]

for _x in list(TEST_SITES):
    for _k, _nice in _PROMOTE.items():
        if (_k in _norm(_x.get("name")) or _k == _norm(_x.get("id"))) and not any(p["id"] == _k for p in PROMOTED_SOURCES):
            PROMOTED_SOURCES.append({"id": _k, "name": _nice, "url": _x["feed"], "group": "general"})

# ePornHome is removed from the test list
TEST_SITES[:] = [x for x in TEST_SITES if "epornhome" not in _norm(x.get("name")) + _norm(x.get("id"))]


def _reg_host(url):
    h = (urlparse(url if "://" in (url or "") else "https://" + (url or "")).hostname or "").lower()
    return ".".join(h.split(".")[-2:]) if h else ""


# Hosts served by the extra (sourcetest / extras) scrapers instead of the generic one.
_PLUS_HOSTS = {_reg_host(x["feed"]) for x in TEST_SITES}
_PLUS_HOSTS |= {_reg_host(p["url"]) for p in PROMOTED_SOURCES}
_PLUS_HOSTS |= {"porno-666.me"}


def _use_plus(url):
    try:
        if _reg_host(url) in _PLUS_HOSTS:
            return True
        return bool(is_smart(url))
    except Exception:
        return False


def _scrape_plus_one(u):
    url, pn = (u.get("url"), u.get("page_num")) if isinstance(u, dict) else (u, None)
    try:
        r = scrape_plus(url, max_items=80) or {}
    except Exception as e:
        return {"page": url, "items": [], "count": 0, "error": str(e), "next_page": None, "page_num": pn}
    items = r.get("items") or []
    r.setdefault("page", url)
    r["items"] = items
    r["count"] = len(items)
    r["page_num"] = pn if pn is not None else r.get("page_num") or _current_page_number(url)
    if items and not r.get("next_page"):
        r["next_page"] = _guess_next_page(url, r["page_num"])
        r["next_is_guess"] = True
    return r


def _plus_search(site, query):
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

# Comma-separated list of allowed frontends, e.g. "https://porn-archive.pages.dev"
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
CORS(
    app,
    origins=_origins if _origins != ["*"] else "*",
    max_age=600,
    expose_headers=["Content-Type", "Content-Length", "Content-Range",
                    "Accept-Ranges", "Content-Disposition"],
)


def _body():
    return request.get_json(silent=True) or {}


def _err(msg, code=400, **extra):
    return jsonify({"error": msg, **extra}), code


# ---------------------------------------------------------------- SSRF guard
def _is_public_host(host: str) -> bool:
    """Refuse to proxy to private/loopback/link-local addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


# ---------------------------------------------------------------- JSON API
# Where the browse tabs get their data from.
CATALOG_URLS = {
    "categories": "https://www.superporn.com/categories",     # general categories
    "pornstars": "https://www.freesexvideos.xxx/models/",     # pornstars / models
    "studios": "https://www.freesexvideos.xxx/sites/",        # studios (title + videos + See all)
    "bdsmStudios": "https://www.bdsmhole.com/studios/",        # BDSM studios (Networks tab, BDSM filter)
    "liveCams": "https://www.lemoncams.com/",                  # Sex Cams tab (live-stream cards only)
}


@app.get("/api/catalog-urls")
def api_catalog_urls():
    return jsonify(CATALOG_URLS)


@app.get("/")
def index():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        data = json.dumps(PROMOTED_SOURCES).replace("</", "<\\/")
        cfg = json.dumps(CATALOG_URLS).replace("</", "<\\/")
        html = html.replace(
            "</head>",
            f"<script>window.__PROMOTED_SOURCES__={data};window.__CATALOG_URLS__={cfg};</script></head>", 1)
        return Response(html, mimetype="text/html")
    except Exception:
        return send_file(path)


@app.get("/api/test-sites")
def api_test_sites():
    return jsonify({"sites": [{"id": x["id"], "name": x["name"], "feed": x["feed"]} for x in TEST_SITES]})


def _site(d):
    """Built-in site by id, otherwise a custom site built from the URL the user typed."""
    known = next((x for x in TEST_SITES if x["id"] == d.get("id")), None)
    if known:
        return known
    c = d.get("site") or {}
    feed = (c.get("feed") or "").strip()
    if feed and " " not in feed and "." in feed:
        return site_from_url(feed, (c.get("name") or "").strip() or None)
    return None


@app.post("/api/test-run")
def api_test_run():
    d = _body(); site = _site(d)
    if not site:
        return _err("unknown site")
    try:
        return jsonify(test_site(site, d.get("query") or "milf"))
    except Exception as e:
        return _err("test failed", 500, detail=str(e))


@app.post("/api/test-feed")
def api_test_feed():
    d = _body(); site = _site(d)
    if not site:
        return _err("unknown site")
    return jsonify(scrape_plus(d.get("url") or site["feed"], max_items=80))


@app.post("/api/test-search")
def api_test_search():
    d = _body(); site = _site(d); q = (d.get("query") or "").strip()
    if not site or not q:
        return _err("id and query required")
    return jsonify(search_site(site, q))


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- page cache
# A scraped listing page is reused for a few minutes: going back to a site / category, or the
# frontend fetching several pages ahead while searching, never scrapes the same page twice.
_PAGE_CACHE = {}                 # (url, page_num) -> (timestamp, result)
_PAGE_TTL, _PAGE_MAX = 600, 600
_PAGE_LOCK = threading.Lock()


def _url_of(u):
    return u.get("url") if isinstance(u, dict) else u


def _page_key(u):
    return (_url_of(u), u.get("page_num") if isinstance(u, dict) else None)


def _page_cache_get(u):
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(_page_key(u))
    if hit and time.time() - hit[0] < _PAGE_TTL:
        return hit[1]
    return None


def _page_cache_put(u, r):
    if not isinstance(r, dict) or r.get("error") or not r.get("items"):
        return                     # never cache failures / empty pages: they should be retried
    with _PAGE_LOCK:
        if len(_PAGE_CACHE) >= _PAGE_MAX:
            for k in sorted(_PAGE_CACHE, key=lambda k: _PAGE_CACHE[k][0])[: _PAGE_MAX // 4]:
                _PAGE_CACHE.pop(k, None)
        _PAGE_CACHE[_page_key(u)] = (time.time(), r)


@app.post("/api/scrape")
def api_scrape():
    started = time.perf_counter()
    data = _body()
    urls = data.get("urls") or ([data["url"]] if data.get("url") else [])
    if not urls:
        return _err("No urls provided")
    urls = urls[:12]
    try:
        results = [None] * len(urls)
        todo = []
        cache_hits = 0
        for i, u in enumerate(urls):
            hit = None if data.get("fresh") else _page_cache_get(u)
            if hit is not None:
                results[i] = hit
                cache_hits += 1
            else:
                todo.append(i)
        normal = [i for i in todo if not _use_plus(_url_of(urls[i]))]
        normal_set = set(normal)
        plus = [i for i in todo if i not in normal_set]
        with ThreadPoolExecutor(max_workers=8) as pool:
            # extra-scraper sites run in parallel with each other AND with the generic scraper
            plus_futs = [(i, pool.submit(_scrape_plus_one, urls[i])) for i in plus]
            if normal:
                for i, r in zip(normal, scrape_many([urls[i] for i in normal])):
                    results[i] = r
            for i, f in plus_futs:
                results[i] = f.result()
            # translate the freshly scraped pages in parallel, then cache the final result
            fresh_idx = todo
            done = list(pool.map(lambda i: translate_result(results[i]) if isinstance(results[i], dict) else results[i], fresh_idx))
            for i, r in zip(fresh_idx, done):
                results[i] = r
                _page_cache_put(urls[i], r)
        app.logger.info(
            "scrape complete pages=%d cache_hits=%d cache_misses=%d fresh=%s elapsed_ms=%d",
            len(urls), cache_hits, len(todo), bool(data.get("fresh")),
            round((time.perf_counter() - started) * 1000),
        )
    except Exception as e:
        app.logger.exception(
            "scrape failed pages=%d elapsed_ms=%d",
            len(urls), round((time.perf_counter() - started) * 1000),
        )
        return _err("Scrape failed", 500, detail=str(e))
    return jsonify({"results": results})


# ---------------------------------------------------------------- search cache
_SEARCH_CACHE = {}               # (sites, query, verify, max_items) -> (timestamp, payload)
_SEARCH_TTL, _SEARCH_MAX = 300, 200
_SEARCH_CACHE_LOCK = threading.Lock()


@app.post("/api/search")
def api_search():
    """Multi-site search. Returns the per-site results (backwards compatible) plus a
    `combined` list: every unique video across all sites, ranked cross-site so clients
    can show ONE flat result list. `verify:true` drops videos whose page is dead.
    Results are cached for a few minutes (same query = instant)."""
    data = _body()
    sites = [s for s in (data.get("sites") or []) if isinstance(s, str)]
    query = (data.get("query") or "").strip()
    verify = bool(data.get("verify"))
    try:
        max_items = min(80, max(1, int(data.get("max_items") or 40)))
    except (TypeError, ValueError):
        return _err("max_items must be an integer")
    if not sites or not query:
        return _err("sites and query are required")
    sites = sites[:40]
    ckey = (tuple(sorted(sites)), query, verify, max_items)
    now = time.time()
    with _SEARCH_CACHE_LOCK:
        hit = _SEARCH_CACHE.get(ckey)
        if hit and now - hit[0] >= _SEARCH_TTL:
            _SEARCH_CACHE.pop(ckey, None)
            hit = None
    if hit and now - hit[0] < _SEARCH_TTL:
        return jsonify(hit[1])
    try:
        results = [None] * len(sites)
        normal = [(i, x) for i, x in enumerate(sites) if not _use_plus(x)]
        plus = [(i, x) for i, x in enumerate(sites) if _use_plus(x)]
        with ThreadPoolExecutor(max_workers=min(3, len(plus)) or 1) as pool:
            plus_futures = {i: pool.submit(_plus_search, x, query) for i, x in plus}
            if normal:
                for (i, _), r in zip(normal, search_many([x for _, x in normal], query,
                                                         max_items=max_items, verify=verify)):
                    results[i] = r
        for i, future in plus_futures.items():
            results[i] = future.result()
    except Exception as e:
        return _err("Search failed", 500, detail=str(e))
    results = [translate_result(r) if isinstance(r, dict) else r for r in results]

    # "search everything": rank on title, tags, genres, stars, studios, description and URL (searchkit = twin of the Worker's search.ts);
    # the site's own search results stay (it matched them on something), so a tag-only hit is not thrown away
    combined = rank_combined([r for r in results if isinstance(r, dict)], parse_query(query))
    payload = {"results": results, "query": query, "combined": combined,
               "count": len(combined)}
    with _SEARCH_CACHE_LOCK:
        expired = [k for k, value in _SEARCH_CACHE.items() if now - value[0] >= _SEARCH_TTL]
        for k in expired:
            _SEARCH_CACHE.pop(k, None)
        if len(_SEARCH_CACHE) >= _SEARCH_MAX:
            oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
            _SEARCH_CACHE.pop(oldest, None)
        _SEARCH_CACHE[ckey] = (now, payload)
    return jsonify(payload)


@app.post("/api/resolve")
def api_resolve():
    url = _body().get("url")
    if not url:
        return _err("url is required")
    try:
        r = deep_resolve(url, max_fetch=5) if is_smart(url) else resolve_video_url(url, light=True)
        if not r.get("video") and not r.get("error") and not is_smart(url):
            r = deep_resolve(url, max_fetch=5)
        return jsonify(r)
    except Exception as e:
        return _err("Resolve failed", 500, detail=str(e))


@app.post("/api/resolve-full")
def api_resolve_full():
    url = _body().get("url")
    if not url:
        return _err("url is required")
    try:
        r = deep_resolve(url) if is_smart(url) else resolve_full_video_url(url, fresh=True)
        if not (r or {}).get("video") and not is_smart(url):
            r = deep_resolve(url)
        return jsonify(r)
    except Exception as e:
        return _err("Resolve failed", 500, detail=str(e))


# ---------------------------------------------------------------- video page metadata
_META_CACHE = {}            # url -> (timestamp, payload)
_META_TTL, _META_MAX = 900, 400


@app.post("/api/metadata")
def api_metadata():
    """Duration / date / views / rating + linked genres, stars, series, uploader for one video page."""
    url = (_body().get("url") or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return _err("valid url is required")
    if not _is_public_host(p.hostname):
        return _err("Blocked host", 403)

    now = time.time()
    hit = _META_CACHE.get(url)
    if hit and now - hit[0] < _META_TTL:
        return jsonify(hit[1])
    try:
        html, final = fetch_html(url, timeout=20.0, referer=f"{p.scheme}://{p.netloc}/")
        data = fetch_metadata(html, final)
    except Exception as e:
        # not cached: a transient failure should be retried next time
        return jsonify({"url": url, "groups": [], "error": f"{type(e).__name__}: {e}"})
    if len(_META_CACHE) >= _META_MAX:
        for k in sorted(_META_CACHE, key=lambda k: _META_CACHE[k][0])[: _META_MAX // 4]:
            _META_CACHE.pop(k, None)
    _META_CACHE[url] = (now, data)
    return jsonify(data)


@app.post("/api/livecams")
def api_livecams():
    """Live-stream cards from lemoncams.com (header, footer and sidebar are never read)."""
    d = _body()
    try:
        return jsonify(fetch_livecams(d.get("url"), force=bool(d.get("force"))))
    except Exception as e:
        return _err("Live cams failed", 500, detail=str(e))


@app.post("/api/channels")
def api_channels():
    """Live TV channel cards from xlivetv.com, one page per call (the frontend scrolls through the pages)."""
    d = _body()
    try:
        return jsonify(fetch_channels(d.get("category"), d.get("page") or 1, force=bool(d.get("force"))))
    except Exception as e:
        return _err("Live channels failed", 500, detail=str(e))


@app.get("/api/cam-thumb")
def api_cam_thumb():
    """Cam thumbnail fetched server-side with the platform's own Referer (their CDNs block hot-linking)."""
    u = (request.args.get("u") or "").strip()
    if not u:
        return _err("u is required")
    try:
        body, ctype = fetch_thumb(u)
    except LookupError:
        return _err("unknown thumbnail", 404)
    except Exception as e:
        return _err("thumbnail unavailable", 502, detail=str(e))
    resp = Response(body, mimetype=ctype)
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp


@app.post("/api/translate-titles")
def api_translate_titles():
    """Batch-translate titles (used by the frontend for leftovers still in Russian)."""
    data = _body()
    texts = data.get("texts") or data.get("titles") or []
    if not isinstance(texts, list):
        return _err("texts must be a list")
    texts = [str(t) for t in texts[:80]]
    try:
        from translate_titles import translate_texts_list
        out = translate_texts_list(texts)
        return jsonify({"titles": out, "count": len(out)})
    except Exception as e:
        return _err("Translate failed", 500, detail=str(e))


@app.post("/api/scrape-categories")
def api_scrape_categories():
    data = _body()
    url = data.get("url")
    mode = data.get("mode")
    if not url:
        return _err("url is required")
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        host, path = (p.hostname or "").lower(), (p.path or "/").lower()
        # Dedicated scrapers, picked by mode or recognised from the URL itself
        if mode in ("models", "pornstars") or (host.endswith("freesexvideos.xxx") and path.startswith("/models")):
            return jsonify(translate_result(scrape_models(url, page_num=data.get("page_num"))))
        if host.endswith("superporn.com") and re.match(r"^/categories(/\d+)?/?$", path):
            return jsonify(translate_result(scrape_superporn_categories(url, page_num=data.get("page_num"))))
        if mode == "sites" or (host.endswith("freesexvideos.xxx") and re.match(r"^/sites(/\d+)?/?$", path)):
            return jsonify(translate_result(scrape_studio_sections(url)))
        if mode == "tags":
            return jsonify(translate_result(scrape_tags(url, kind="porntags")))
        return jsonify(translate_result(scrape_categories(url, page_num=data.get("page_num"))))
    except Exception as e:
        return _err("Category scrape failed", 500, detail=str(e))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)