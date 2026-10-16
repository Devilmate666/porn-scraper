"""Fallbacks: WordPress-style card feeds/thumbnails + deep video resolution through embed pages."""
import base64
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scraper import (_http_client, HEADERS, scrape_page, resolve_video_url,
                     _classify, _is_image_url)

_BAD = re.compile(
    r"/(category|categories|tag|tags|page|author|wp-content|wp-json|feed|login|register|contact|"
    r"dmca|privacy|terms|models?|pornstars?|actors?|channels?|studios?|series|search)(/|$)"
    r"|[?&](s|q)=|\.(jpg|jpeg|png|gif|webp|css|js|svg)(\?|$)", re.I)
_AD = ("preview", "trailer", "/ads/", "banner", "promo", "teaser", "vidthumb", "mediabook", "project1content")
_EMBED_HOST = re.compile(
    r"https?:\\?/\\?/(?:[\w-]+\.)?(?:dood\w*|d0\w+|streamtape|streamwish|wishfast|lulustream|vidara|"
    r"upvideo|streamsb|sbembed|sbplay|filemoon|voe|mixdrop|vidhide|vinovo|luluvdo|hglink|"
    r"streamhub|mp4upload|ok\.ru|fembed|xstreamcdn)\.[a-z]{2,}(?:\\?/[\w./?=&%~:-]+)", re.I)


def _img_url(img, base):
    for a in ("data-src", "data-lazy-src", "data-original", "data-thumb", "data-srcset",
              "data-lazy-srcset", "srcset", "src"):
        v = (img.get(a) or "").strip()
        if not v or v.startswith("data:"):
            continue
        if "srcset" in a or "," in v:
            v = v.split(",")[0].split()[0]
        return urljoin(base, v)
    return None


def card_items(html, base, max_items=80):
    """Any link that wraps a thumbnail image and points to a video-like page on the same host."""
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base).netloc
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.netloc != host or _BAD.search(href):
            continue
        slug = p.path.strip("/").rsplit("/", 1)[-1]
        if len(slug) < 6 or href.rstrip("/") == base.rstrip("/") or href in seen:
            continue
        img = a.find("img")
        if not img:
            continue
        thumb = _img_url(img, base)
        if not thumb:
            continue
        title = (a.get("title") or img.get("alt") or "").strip()
        if not title:
            box = a.parent
            for _ in range(3):
                h = box.find(["h1", "h2", "h3", "h4", "h5"]) if box else None
                if h and h.get_text(strip=True):
                    title = h.get_text(" ", strip=True)
                    break
                box = box.parent if box else None
        title = title or slug.replace("-", " ").title()
        seen.add(href)
        items.append({"title": title, "link": href, "thumbnail": thumb})
        if len(items) >= max_items:
            break
    return items


def scrape_plus(url, max_items=80, page_num=None):
    res = scrape_page(url, max_items=max_items, page_num=page_num)
    items = res.get("items") or []
    if items and sum(1 for i in items if i.get("thumbnail")) * 2 >= len(items):
        return res
    try:
        with _http_client(headers=HEADERS, follow_redirects=True, timeout=20) as c:
            r = c.get(url)
            r.raise_for_status()
            cards = card_items(r.text, str(r.url), max_items)
    except Exception as e:
        res.setdefault("error", str(e))
        return res
    if not items and cards:
        res.update(items=cards, count=len(cards), fallback="cards")
        res.pop("error", None)
    elif cards:
        tm = {c["link"].split("?")[0]: c["thumbnail"] for c in cards}
        for i in items:
            if not i.get("thumbnail"):
                i["thumbnail"] = tm.get((i.get("link") or "").split("?")[0])
    return res


# ---------------------------------------------------------------- deep video resolving
def unpack(js):
    """Dean Edwards p.a.c.k.e.r unpacker (used by streamwish/lulustream/vidhide-style players)."""
    m = re.search(r"\}\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", js, re.S)
    if not m:
        return None
    payload, radix, count, words = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def enc(n):
        s = ""
        while True:
            s = digits[n % radix] + s
            n //= radix
            if n == 0:
                return s
    lookup = {enc(i): (words[i] if i < len(words) and words[i] else enc(i)) for i in range(count)}
    return re.sub(r"\b\w+\b", lambda x: lookup.get(x.group(0), x.group(0)),
                  payload.replace("\\'", "'"))


def _expand(text):
    extra = []
    for chunk in text.split("eval(function(p,a,c,k,e,")[1:]:
        u = unpack(chunk.split("</script>")[0])
        if u:
            extra.append(u)
    for m in re.finditer(r"atob\(\s*[\"']([A-Za-z0-9+/=]{24,})[\"']\s*\)", text):
        try:
            extra.append(base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore"))
        except Exception:
            pass
    return text + "\n" + "\n".join(extra)


def _find_media(text, base):
    text = re.sub(r"data-(?:trailer|preview|video-preview)=\"[^\"]*\"", "", text)
    t = text.replace("\\/", "/").replace("&amp;", "&").replace("\\u0026", "&")
    out = [m.group(0) for m in re.finditer(
        r"https?://[^\s\"'<>\\)]+?\.(?:m3u8|mp4|webm)(?:\?[^\s\"'<>\\)]*)?", t, re.I)]
    for m in re.finditer(r"(?:file|src|source|hls\d*|video_url|contentUrl)[\"']?\s*[:=]\s*"
                         r"[\"']((?:https?:)?//[^\"']+|/[^\"']+\.(?:m3u8|mp4))[\"']", t, re.I):
        out.append(urljoin(base, m.group(1)))
    return out


def _kvs(text, base):
    """KVS engine flashvars: video_url / video_alt_url[N] with *_text quality labels."""
    out = []
    for m in re.finditer(r"(video_url|video_alt_url\d*)\s*:\s*'([^']+)'", text):
        key, u = m.groups()
        if u.startswith("function/"):
            continue
        lab = re.search(key + r"_text\s*:\s*'([^']*)'", text)
        label = lab.group(1) if lab else ""
        hm = re.match(r"\d+", label)
        out.append({"src": urljoin(base, u), "type": "mp4", "label": label,
                    "height": int(hm.group(0)) if hm else 0})
    out.sort(key=lambda x: -x["height"])
    return out


def _data_source(text):
    """data-source="<reversed base64 of a signed m3u8/mp4 url>" (epornhome-style)."""
    out = []
    for m in re.finditer(r'data-source="([A-Za-z0-9+/=_-]{40,})"', text):
        for v in (m.group(1)[::-1], m.group(1)):
            try:
                d = base64.b64decode(v + "=" * (-len(v) % 4)).decode("latin-1")
            except Exception:
                continue
            out += re.findall(r"https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+", d)
    return out


def _sources(text, base):
    found = list(_kvs(text, base))
    for u in _data_source(text) + _find_media(text, base):
        low = u.lower()
        if _classify(u) != "other" and not _is_image_url(u) and not any(x in low for x in _AD):
            found.append({"src": u, "type": _classify(u)})
    return found


_LAZY = ("src", "data-src", "data-lazy-src", "data-litespeed-src", "data-lazy", "data-original")


SMART_HOSTS = ("xfuntaxy.com", "sxyprn.com", "pornhd8k.me", "erofans.net", "hornysimp.com",
               "epornhome.com", "porno-666.me", "tlenporno.com")


def is_smart(url):
    h = urlparse(url).netloc.lower()
    return any(h == d or h.endswith("." + d) for d in SMART_HOSTS)


def deep_resolve(detail_url, max_fetch=8):
    # these sites hide trailers/ads in the page, so skip the generic resolver and read them directly
    r = {} if is_smart(detail_url) else (resolve_video_url(detail_url, fresh=True) or {})
    v = r.get("video")
    if v and not any(x in (v.get("src") or "").lower() for x in _AD) and "/get_file/" not in (v.get("src") or ""):
        return {**r, "via": "page"}
    queue, seen, found = [(detail_url, None)], set(), []
    while queue and len(seen) < max_fetch:
        url, ref = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            with _http_client(headers=HEADERS, follow_redirects=True, timeout=15) as c:
                resp = c.get(url, headers={**HEADERS, **({"Referer": ref} if ref else {})})
                base, text = str(resp.url), _expand(resp.text)
        except Exception:
            continue
        for x in _sources(text, base):
            found.append({**x, "embed": url})
        if found:
            break
        soup = BeautifulSoup(text, "lxml")
        for fr in soup.find_all(["iframe", "embed"]):
            src = next((fr.get(a) for a in _LAZY if fr.get(a) and not fr.get(a).startswith(("about:", "javascript"))), None)
            if src:
                queue.append((urljoin(base, src), url))
        for m in _EMBED_HOST.finditer(text):
            queue.append((m.group(0).replace("\\/", "/"), url))
    if found:
        found.sort(key=lambda x: (-(x.get("height") or 0), 0 if x["type"] == "mp4" else 1))
        return {"detail_url": detail_url, "video": found[0], "all": found, "unavailable": False,
                "thumbnail": r.get("thumbnail"), "via": "embed"}
    r = dict(r)
    r["video"], r["all"] = None, []          # only previews/ads were found - not playable
    r["embed_urls"] = [u for u in seen if u != detail_url]
    return r
