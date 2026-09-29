"""Flask backend: JSON scraping API + a range-aware media proxy for playback/downloads.

Routes (match what index.html calls):
  POST /api/scrape             {urls:[str|{url,page_num}], download:false}
  POST /api/search             {sites:[...], query}
  POST /api/resolve            {url}
  POST /api/resolve-full       {url}
  POST /api/scrape-categories  {url, mode?: 'sites'|'tags'}
  GET  /api/stream             ?url=&referer=&download=1&filename=
  GET  /healthz
"""
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse, quote

import httpx
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

from scraper import (
    scrape_many, resolve_video_url, resolve_full_video_url, search_many,
    scrape_categories, scrape_tags, scrape_studio_sections, HEADERS,
)

app = Flask(__name__)

# Comma-separated list of allowed frontends, e.g. "https://porn-archive.pages.dev"
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
CORS(
    app,
    origins=_origins if _origins != ["*"] else "*",
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
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/scrape")
def api_scrape():
    data = _body()
    urls = data.get("urls") or ([data["url"]] if data.get("url") else [])
    if not urls:
        return _err("No urls provided")
    urls = urls[:12]
    try:
        results = scrape_many(urls, download=bool(data.get("download")))
    except Exception as e:
        return _err("Scrape failed", 500, detail=str(e))
    return jsonify({"results": results})


@app.post("/api/search")
def api_search():
    data = _body()
    sites = data.get("sites") or []
    query = (data.get("query") or "").strip()
    if not sites or not query:
        return _err("sites and query are required")
    try:
        results = search_many(sites[:40], query)
    except Exception as e:
        return _err("Search failed", 500, detail=str(e))
    return jsonify({"results": results, "query": query})


@app.post("/api/resolve")
def api_resolve():
    url = _body().get("url")
    if not url:
        return _err("url is required")
    try:
        return jsonify(resolve_video_url(url, light=True))
    except Exception as e:
        return _err("Resolve failed", 500, detail=str(e))


@app.post("/api/resolve-full")
def api_resolve_full():
    url = _body().get("url")
    if not url:
        return _err("url is required")
    try:
        return jsonify(resolve_full_video_url(url, fresh=True))
    except Exception as e:
        return _err("Resolve failed", 500, detail=str(e))


@app.post("/api/scrape-categories")
def api_scrape_categories():
    data = _body()
    url = data.get("url")
    mode = data.get("mode")
    if not url:
        return _err("url is required")
    try:
        if mode == "sites":
            return jsonify(scrape_studio_sections(url))
        if mode == "tags":
            return jsonify(scrape_tags(url, kind="porntags"))
        return jsonify(scrape_categories(url, page_num=data.get("page_num")))
    except Exception as e:
        return _err("Category scrape failed", 500, detail=str(e))


# ---------------------------------------------------------------- media proxy
_PASS_REQ = ("Range", "If-Range", "If-None-Match", "If-Modified-Since")
_PASS_RESP = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
              "ETag", "Last-Modified", "Cache-Control")


@app.get("/api/stream")
def api_stream():
    src = request.args.get("url", "")
    referer = request.args.get("referer", "")
    download = request.args.get("download") == "1"
    filename = request.args.get("filename") or "video.mp4"

    p = urlparse(src)
    if p.scheme not in ("http", "https") or not p.hostname:
        return _err("Invalid url")
    if not _is_public_host(p.hostname):
        return _err("Blocked host", 403)

    headers = dict(HEADERS)
    headers["Accept"] = "*/*"
    headers["Referer"] = referer or f"{p.scheme}://{p.netloc}/"
    try:
        rp = urlparse(headers["Referer"])
        headers["Origin"] = f"{rp.scheme}://{rp.netloc}"
    except Exception:
        pass
    for h in _PASS_REQ:
        if request.headers.get(h):
            headers[h] = request.headers[h]

    client = httpx.Client(follow_redirects=True,
                          timeout=httpx.Timeout(30.0, connect=10.0, read=60.0))
    try:
        req = client.build_request("GET", src, headers=headers)
        upstream = client.send(req, stream=True)
    except Exception as e:
        client.close()
        return _err("Upstream unreachable", 502, detail=str(e))

    if upstream.status_code >= 400 and upstream.status_code != 416:
        code = upstream.status_code
        upstream.close()
        client.close()
        return _err("Upstream error", 502, detail=f"HTTP {code}")

    def gen():
        try:
            for chunk in upstream.iter_bytes(64 * 1024):
                yield chunk
        finally:
            upstream.close()
            client.close()

    resp = Response(stream_with_context(gen()), status=upstream.status_code)
    for h in _PASS_RESP:
        v = upstream.headers.get(h)
        if v:
            resp.headers[h] = v
    if "Accept-Ranges" not in resp.headers:
        resp.headers["Accept-Ranges"] = "bytes"
    if not resp.headers.get("Content-Type"):
        resp.headers["Content-Type"] = "video/mp4"
    resp.headers["X-Accel-Buffering"] = "no"
    if download:
        safe = re.sub(r'[\r\n"\\]', "", filename)
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=\"{safe.encode('ascii', 'ignore').decode() or 'video.mp4'}\"; "
            f"filename*=UTF-8''{quote(safe)}"
        )
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
