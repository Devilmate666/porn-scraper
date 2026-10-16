"""Source tester: for each site checks feed, search and whether a video is playable WITHOUT a proxy.
Run:  python sourcetest.py            (prints a report for all sites)
"""
import sys
from urllib.parse import quote_plus

from scraper import resolve_video_url
from extras import scrape_plus as scrape_page, deep_resolve

# feed = first page to list; search = URL templates tried in order ({q} = query)
TEST_SITES = [
    {"id": "porno666", "name": "Porno-666", "feed": "https://m.porno-666.me/",
     "search": ["https://m.porno-666.me/?s={q}", "https://m.porno-666.me/search/{q}/"]},
    {"id": "epornhome", "name": "ePornHome", "feed": "https://epornhome.com/",
     "search": ["https://epornhome.com/?s={q}", "https://epornhome.com/search/{q}/"]},
    {"id": "xfuntaxy", "name": "Xfuntaxy", "feed": "https://xfuntaxy.com/",
     "search": ["https://xfuntaxy.com/?s={q}", "https://xfuntaxy.com/search/{q}/"]},
    {"id": "tlenporno", "name": "TlenPorno", "feed": "https://web.tlenporno.com/",
     "search": ["https://web.tlenporno.com/?s={q}", "https://web.tlenporno.com/search/{q}/"]},
    {"id": "pornvideobb", "name": "PornVideoBB", "feed": "https://2023.pornvideobb.com/",
     "search": ["https://2023.pornvideobb.com/?s={q}", "https://2023.pornvideobb.com/search/{q}/"]},
]


def site_from_url(feed, name=None):
    """Build a testable site from any URL, with the usual search URL patterns."""
    from urllib.parse import urlparse
    if not feed.startswith("http"):
        feed = "https://" + feed
    o = urlparse(feed)
    origin = f"{o.scheme}://{o.netloc}"
    return {"id": o.netloc.replace(".", "-"), "name": name or o.netloc.replace("www.", ""), "feed": feed,
            "search": [origin + "/?s={q}", origin + "/search/{q}/", origin + "/search/{q}",
                       origin + "/search?q={q}", origin + "/?q={q}", origin + "/videos/search?q={q}"]}


def _links(items):
    return {(i.get("link") or "").split("?")[0] for i in items if i.get("link")}


def search_site(site, query, max_items=60):
    """First search template that returns items different from the plain feed."""
    try:
        base = _links(scrape_page(site["feed"], max_items=max_items).get("items", []))
    except Exception:
        base = set()
    last = {"items": [], "error": "no template worked"}
    for tpl in site["search"]:
        url = tpl.replace("{q}", quote_plus(query))
        try:
            res = scrape_page(url, max_items=max_items)
        except Exception as e:
            res = {"items": [], "error": str(e)}
        res["search_url"] = url
        last = res
        items = res.get("items") or []
        if items and (not base or len(_links(items) & base) / max(len(_links(items)), 1) < 0.9):
            return res
    last["items"] = []
    return last


def classify_video(detail_url):
    """direct = mp4/hls/webm the browser can play itself; embed = only an iframe host; none."""
    try:
        r = deep_resolve(detail_url)
    except Exception as e:
        return {"kind": "error", "detail": str(e)}
    v = (r or {}).get("video")
    if v:
        return {"kind": "direct", "type": v.get("type"), "src": v.get("src")}
    hosts = sorted({u.split("/")[2] for u in (r or {}).get("embed_urls", []) if u.startswith("http")})
    return {"kind": "none", "detail": (r or {}).get("error") or ("embed only: " + ", ".join(hosts) if hosts else "no media found")}


def test_site(site, query="milf"):
    out = {"id": site["id"], "name": site["name"], "feed_count": 0, "search_count": 0,
           "videos": [], "error": None}
    feed = scrape_page(site["feed"], max_items=40)
    items = feed.get("items") or []
    out["feed_count"] = len(items)
    out["error"] = feed.get("error")
    out["with_thumb"] = sum(1 for i in items if i.get("thumbnail"))
    for it in items[:3]:
        out["videos"].append({"link": it.get("link"), **classify_video(it["link"])})
    s = search_site(site, query)
    out["search_count"] = len(s.get("items") or [])
    out["search_url"] = s.get("search_url")
    direct = sum(1 for v in out["videos"] if v["kind"] == "direct")
    out["verdict"] = ("KEEP" if out["feed_count"] and direct and out["search_count"]
                      else "PARTIAL" if out["feed_count"] and (direct or out["search_count"])
                      else "DROP")
    return out


if __name__ == "__main__":
    only = set(sys.argv[1:])
    for site in TEST_SITES:
        if only and site["id"] not in only:
            continue
        r = test_site(site)
        print(f"\n== {r['name']}: {r['verdict']}  feed={r['feed_count']} (thumbs {r.get('with_thumb')}) "
              f"search={r['search_count']}" + (f"  ERROR: {r['error']}" if r['error'] else ""))
        for v in r["videos"]:
            print(f"   {v['kind']:6} {v.get('type') or ''} {(v.get('src') or v.get('detail') or '')[:90]}")
