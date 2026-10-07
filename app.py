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
from scraper import scrape_page, _guess_next_page, _current_page_number, fetch_html
try:
    from metadata import fetch_metadata
except Exception as _e:          # metadata is a bonus: never take the whole app down
    print(f"[metadata] disabled: {_e}", flush=True)
    def fetch_metadata(html, url):
        return {"url": url, "groups": [], "error": "metadata module unavailable"}
try:                              # optional fallback video resolver; the app runs without it
    from extras import deep_resolve, is_smart
except Exception:
    def is_smart(url):
        return False

    def deep_resolve(url, max_fetch=5):
        return {"video": None, "error": "deep resolver not installed"}
try:
    from livecams import fetch_livecams, fetch_thumb
except Exception as _e:          # the cams tab is a bonus: never take the whole app down
    print(f"[livecams] disabled: {_e}", flush=True)
    def fetch_livecams(url=None, force=False, _why=str(_e)):      # `_e` is deleted when the except block ends: capture it now
        return {"items": [], "count": 0, "error": "livecams module unavailable", "diagnostics": [_why]}

    def fetch_thumb(url):
        raise RuntimeError("livecams module unavailable")
try:
    from channels import fetch_channels
except Exception as _e:          # the channels tab is a bonus: never take the whole app down
    print(f"[channels] disabled: {_e}", flush=True)
    def fetch_channels(category=None, page=1, force=False, _why=str(_e)):
        return {"items": [], "count": 0, "error": "channels module unavailable", "diagnostics": [_why]}

app = Flask(__name__)

# Only the built-in sites exist: superporn, pornvideobb, freesexvideos, bdsmhole (+ cams / live TV). No extra or test sites.
PROMOTED_SOURCES = []           # the page still reads window.__PROMOTED_SOURCES__; it is simply empty


def _reg_host(url):
    h = (urlparse(url if "://" in (url or "") else "https://" + (url or "")).hostname or "").lower()
    return ".".join(h.split(".")[-2:]) if h else ""


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
        if todo:
            for i, r in zip(todo, scrape_many([urls[i] for i in todo])):
                results[i] = r
                _page_cache_put(urls[i], r)                  # cache the freshly scraped page
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
        results = list(search_many(sites, query, max_items=max_items, verify=verify))
    except Exception as e:
        return _err("Search failed", 500, detail=str(e))

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
            return jsonify(scrape_models(url, page_num=data.get("page_num")))
        if host.endswith("superporn.com") and re.match(r"^/categories(/\d+)?/?$", path):
            return jsonify(scrape_superporn_categories(url, page_num=data.get("page_num")))
        if mode == "sites" or (host.endswith("freesexvideos.xxx") and re.match(r"^/sites(/\d+)?/?$", path)):
            return jsonify(scrape_studio_sections(url))
        if mode == "tags":
            return jsonify(scrape_tags(url, kind="porntags"))
        return jsonify(scrape_categories(url, page_num=data.get("page_num")))
    except Exception as e:
        return _err("Category scrape failed", 500, detail=str(e))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)