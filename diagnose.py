"""Saves the raw HTML of each site's feed, first video pages and player iframes to ./debug and zips it.
Upload debug.zip (or just one site's files) so the parsers can be fixed against the real pages."""
import os, re, sys, zipfile
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scraper import _http_client, HEADERS
from sourcetest import TEST_SITES
from extras import card_items, scrape_plus

os.makedirs("debug", exist_ok=True)
SKIP = {"pornvideobb"}          # already working

def get(url, ref=None):
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=20) as c:
            r = c.get(url, headers={**HEADERS, **({"Referer": ref} if ref else {})})
            return r.status_code, str(r.url), r.text
    except Exception as e:
        return 0, url, f"ERROR {type(e).__name__}: {e}"

def save(name, url, status, text):
    with open(f"debug/{name}.html", "w", encoding="utf-8") as f:
        f.write(f"<!-- url={url} status={status} bytes={len(text)} -->\n{text}")
    blocked = status in (403, 429, 503) or "Just a moment" in text[:3000]
    print(f"  {name}: HTTP {status}, {len(text)} bytes" + ("  <-- BLOCKED (Cloudflare/anti-bot)" if blocked else ""))

only = set(sys.argv[1:])
for s in TEST_SITES:
    if s["id"] in SKIP or (only and s["id"] not in only):
        continue
    print(f"\n== {s['name']}")
    st, u, html = get(s["feed"]); save(f"{s['id']}_feed", u, st, html)
    links = [i["link"] for i in (scrape_plus(s["feed"], max_items=10).get("items") or [])][:2]
    if not links:
        links = [c["link"] for c in card_items(html, u, 5)][:2]
    print(f"  video links found: {len(links)}")
    for n, link in enumerate(links, 1):
        st, u2, page = get(link, ref=s["feed"]); save(f"{s['id']}_video{n}", u2, st, page)
        soup = BeautifulSoup(page, "lxml")
        frames = [urljoin(u2, f.get("src") or f.get("data-src") or "") for f in soup.find_all("iframe") if (f.get("src") or f.get("data-src"))]
        for k, fr in enumerate(frames[:2], 1):
            st, u3, body = get(fr, ref=link); save(f"{s['id']}_video{n}_embed{k}", u3, st, body)

with zipfile.ZipFile("debug.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for fn in os.listdir("debug"):
        z.write(os.path.join("debug", fn), fn)
print("\nDone -> debug.zip  (upload it here)")
