"""Video page metadata: duration, date, views, rating + linked genres / tags / models / studios.

Everything is defensive. Fully restored and visible in the frontend.
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

from scraper import _is_chrome, _unescape_text

try:                                    # newer scraper builds ship this helper
    from scraper import _norm_dur
except ImportError:
    def _norm_dur(t):
        """'12:34' / '1:02:03' / '12 min' / '1h 5m' -> normalised 'm:ss' or 'h:mm:ss' (or None)."""
        t = (t or "").strip()
        m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", t)
        if m:
            a, b, c = m.group(1), m.group(2), m.group(3)
            return f"{int(a)}:{b}:{c}" if c else f"{int(a)}:{b}"
        h = re.search(r"(\d+)\s*(?:h|hr|hrs|hours?)\b", t, re.I)
        mi = re.search(r"(\d+)\s*(?:m|min|mins|minutes?)\b", t, re.I)
        se = re.search(r"(\d+)\s*(?:s|sec|secs|seconds?)\b", t, re.I)
        if h or mi or se:
            tot = (int(h.group(1)) * 3600 if h else 0) + (int(mi.group(1)) * 60 if mi else 0) + (int(se.group(1)) if se else 0)
            if tot:
                hh, mm, ss = tot // 3600, (tot % 3600) // 60, tot % 60
                return f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm}:{ss:02d}"
        return None


_KIND_RX = [
    ("models", re.compile(r"/(?:models?|pornstars?|porn-stars?|stars?|actors?|actresses?|girls?|performers?)/[^/?#]+", re.I)),
    ("studios", re.compile(r"/(?:studios?|channels?|networks?|sites?|series|brands?|producers?|labels?)/[^/?#]+", re.I)),
    ("uploaders", re.compile(r"/(?:users?|uploaders?|members?|profiles?)/[^/?#]+", re.I)),
    ("categories", re.compile(r"/(?:categor(?:y|ies)|cats?|genres?|niches?)/[^/?#]+", re.I)),
    ("tags", re.compile(r"/(?:tags?|keywords?|topics?)/[^/?#]+", re.I)),
]

_LABELS = {"models": "Pornstars", "studios": "Series / Studios", "uploaders": "Uploader",
           "categories": "Categories", "tags": "Tags"}
_ORDER = ["models", "studios", "uploaders", "categories", "tags"]
_RESERVED = {"login", "signup", "register", "categories", "category", "series", "pornstars", "pornstar", "models",
             "videos", "video", "search", "contact", "tos", "dmca", "terms", "privacy", "cookies-policy", "about",
             "faq", "upload", "live", "new", "popular", "trending", "latest", "best", "top", "favorites", "history",
             "help", "users", "user", "channels", "tags", "sitemap", "cookies", "legal", "explore", "community"}
_JUNK_NAME = re.compile(
    r"^(view all|see all|show all|all|more|show more|load more|next|prev|previous|home|categories|category|"
    r"tags?|models?|pornstars?|studios?|channels?|networks?|sites?|series|»|›|→|\.\.\.)$", re.I)
_BAD_SLUG = re.compile(r"^(page|p|\d+|feed|rss|all)$", re.I)


def _fmt_seconds(sec):
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _to_seconds(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    t = str(v).strip()
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", t, re.I)
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0 for x in m.groups())
        return int(h * 3600 + mi * 60 + s) or None
    if re.fullmatch(r"\d{1,6}(?:\.\d+)?", t):
        return int(float(t)) or None
    d = _norm_dur(t)
    if d:
        parts = [int(x) for x in d.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts))) or None
    return None


def _names(v):
    out = []
    if v is None:
        return out
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,;|]", _unescape_text(v)) if x.strip()]
    if isinstance(v, dict):
        n = v.get("name")
        return [n.strip()] if isinstance(n, str) and n.strip() else []
    if isinstance(v, list):
        for x in v:
            out += _names(x)
    return out


def _clean_name(text):
    t = re.sub(r"\s+", " ", _unescape_text(text)).strip()
    t = re.sub(r"\s*[\(\[]\s*\d[\d,.\s]*[kKmM]?\s*[\)\]]\s*$", "", t)
    t = re.sub(r"\s+\d[\d,.]*[kKmM]?$", "", t) if re.search(r"[A-Za-z\u0400-\u04FF]\s+\d[\d,.]*[kKmM]?$", t) else t
    t = t.strip(" ,;|-–—•·#")
    if not t or len(t) > 45 or len(t) < 2 or _JUNK_NAME.match(t):
        return None
    return t


def _json_ld(soup):
    nodes = []
    for sc in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = (sc.string or sc.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:
                continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                nodes.append(x)
                if "@graph" in x:
                    stack.append(x["@graph"])
    for n in nodes:
        t = n.get("@type")
        t = " ".join(t) if isinstance(t, list) else str(t or "")
        if re.search(r"VideoObject|Movie", t, re.I):
            return n
    return {}


def _meta(soup, *names):
    out = []
    for n in names:
        for tag in soup.find_all("meta", attrs={"property": n}) + soup.find_all("meta", attrs={"name": n}) \
                + soup.find_all("meta", attrs={"itemprop": n}):
            c = (tag.get("content") or "").strip()
            if c:
                out.append(c)
    return out


def _anchor_ok(a):
    if a.find("img"):
        return False
    try:
        if _is_chrome(a) or any(_is_chrome(p) for p in a.parents
                                if getattr(p, "name", None) not in (None, "body", "html", "[document]")):
            return False
    except Exception:
        pass
    return True


def _kind_of(href, a):
    pu = urlparse(href)
    path, query = pu.path, pu.query.lower()
    for kind, rx in _KIND_RX:
        m = rx.search(path)
        if m:
            slug = m.group(0).rstrip("/").rsplit("/", 1)[-1]
            if _BAD_SLUG.match(slug):
                return None
            return kind
    rel = [r.lower() for r in (a.get("rel") or [])]
    if "category" in rel:
        return "categories"
    if "tag" in rel:
        return "tags"
    if re.search(r"(?:^|&)(?:tag|tags)=", query):
        return "tags"
    if re.search(r"(?:^|&)(?:category|cat)=", query):
        return "categories"
    return None


# ----------------------------------------------------------------
# LABELED ROWS  -  "Categories: a b c", "Tags: ...", "Porn star: ...", "Studio: ..."
# The most reliable signal a video page gives: a short label ending in ":" followed by links.
# Works with query-string links (?cat=Anal, ?actor=Jane) and ignores the site-wide menus, which
# have a heading but no colon.
# ----------------------------------------------------------------
_LABEL_KIND = [
    ("models",     r"porn\s*stars?|pornstars?|models?|actors?|actresses?|cast|stars?|performers?|girls?"),
    ("studios",    r"studios?|channels?|networks?|sites?|series|brands?|producers?|labels?|production"),
    ("uploaders",  r"uploaders?|uploaded\s+by|submitted\s+by|posted\s+by|users?|authors?|by"),
    ("categories", r"categor(?:y|ies)|genres?|niches?|sections?"),
    ("tags",       r"tags?|keywords?|topics?"),
]
_LABEL_RX = [(k, re.compile(rf"^\s*(?:{alts})\s*[:\uff1a]\s*$", re.I)) for k, alts in _LABEL_KIND]
_ANY_LABEL_RX = re.compile(r"^\s*[^\W\d_][\w ]{1,24}\s*[:\uff1a]\s*$", re.U)


def _label_kind(text):
    t = re.sub(r"\s+", " ", text or "")
    for kind, rx in _LABEL_RX:
        if rx.match(t):
            return kind
    return None


def _inside(node, scope):
    n = node
    while n is not None:
        if n is scope:
            return True
        n = n.parent
    return False


def _same_site(url, base):
    def reg(u):
        p = (urlparse(u).hostname or "").lower().split(".")
        return ".".join(p[-2:])
    return reg(url) == reg(base)


def _labeled_groups(soup, base):
    """{kind: [{name, link}]} from 'Label:' + links rows. Empty dict when the page has none."""
    out = {}
    for ls in soup.find_all(string=lambda t: isinstance(t, NavigableString) and ":" in t and len(t) <= 40):
        kind = _label_kind(str(ls))
        if not kind:
            continue
        scope = ls.parent
        for _ in range(3):                      # climb until the row also holds the links
            if scope is None:
                break
            if any(_inside(a, scope) for a in ls.find_all_next("a", href=True, limit=3)):
                break
            scope = scope.parent
        if scope is None or getattr(scope, "name", None) in (None, "[document]", "html", "body"):
            continue
        anchors = []
        for el in ls.next_elements:
            if not _inside(el, scope):
                break
            if isinstance(el, NavigableString):
                if el is not ls and _ANY_LABEL_RX.match(str(el)):
                    break                        # the next row's label
                continue
            if getattr(el, "name", None) == "a" and el.get("href"):
                anchors.append(el)
        seen = {x["link"] for x in out.get(kind, [])}
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full = urljoin(base, href).split("#")[0]
            if full in seen or not _same_site(full, base):
                continue
            img = a.find("img")
            name = _clean_name(a.get_text(" ", strip=True) or a.get("title") or (img.get("alt") if img else ""))
            if not name:
                continue
            seen.add(full)
            out.setdefault(kind, []).append({"name": name, "link": full})
            if len(out[kind]) >= 60:
                break
    return out


def _norm_txt(t):
    return re.sub(r"[\W_]+", " ", t or "", flags=re.U).strip().lower()


def _pick_h1(soup):
    """The video's title heading - not the site header's <h1> that often comes first."""
    hs = soup.find_all("h1")
    if len(hs) <= 1:
        return hs[0] if hs else None
    ref = " ".join([_norm_txt(soup.title.get_text()) if soup.title else ""] +
                   [_norm_txt(x) for x in _meta(soup, "og:title")])
    best, best_len = None, 0
    for h in hs:
        t = _norm_txt(h.get_text(" ", strip=True))
        if t and t in ref and len(t) > best_len:
            best, best_len = h, len(t)
    return best if best is not None else max(hs, key=lambda h: len(h.get_text(strip=True)))


def _linked_groups(soup, base):
    found = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base, href).split("#")[0]
        if urlparse(full).netloc.replace("www.", "") != urlparse(base).netloc.replace("www.", ""):
            continue
        if not _anchor_ok(a):
            continue
        kind = _kind_of(full, a)
        if kind:
            found.append((a, full, kind))
    if not found:
        return {}
    pool = found
    h1 = soup.find("h1")
    if h1:
        ids = {id(a) for a, _, _ in found}
        node = h1
        for _ in range(6):
            node = node.parent
            if node is None or node.name in ("body", "html", "[document]"):
                break
            inside = [x for x in node.find_all("a", href=True) if id(x) in ids]
            if len(inside) >= 2:
                keep = {id(x) for x in inside}
                pool = [t for t in found if id(t[0]) in keep]
                break
    groups = {}
    for a, full, kind in pool:
        name = _clean_name(a.get("title") if not a.get_text(strip=True) else a.get_text(" ", strip=True))
        if not name:
            continue
        g = groups.setdefault(kind, {})
        g.setdefault(full.rstrip("/"), {"name": name, "link": full})
    return {k: list(v.values()) for k, v in groups.items() if 0 < len(v) <= 40}


def _flat_tags(soup, base, h1):
    if h1 is None:
        return []
    host = urlparse(base).netloc.replace("www.", "")
    run, seen_other, started = [], 0, False
    for a in h1.find_all_next("a", href=True, limit=140):
        full = urljoin(base, (a.get("href") or "").strip()).split("#")[0]
        pu = urlparse(full)
        slug = pu.path.strip("/").lower()
        flat = (pu.netloc.replace("www.", "") == host and not pu.query
                and re.fullmatch(r"/[a-z0-9][a-z0-9-]{1,40}/?", pu.path or "", re.I)
                and slug not in _RESERVED and not a.find("img") and _anchor_ok(a))
        if flat:
            name = _clean_name(a.get_text(" ", strip=True) or a.get("title"))
            if name:
                started = True
                run.append({"name": name, "link": full})
            continue
        if started:
            break
        seen_other += 1
        if seen_other > 12:
            break
    out, seen = [], set()
    for x in run:
        k = x["link"].rstrip("/")
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out[:20] if len(out) >= 1 else []


def _parse_views(v):
    if isinstance(v, (int, float)):
        return f"{int(v):,}"
    t = str(v).strip()
    return t or None


def _json_ld_video_only(soup):
    return _json_ld(soup)


# --------------------------------------------------------------------------- per-site extractors
# Each of the four built-in sites has its own page layout; these read exactly what the page shows
# (story, genres, tags, stars, series, uploader, duration, views, rating) with the site's own markup.
def _t(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el is not None else ""


def _norm(s):
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def _chips(anchors, base):
    out, seen = [], set()
    for a in anchors:
        href = (a.get("href") or "").strip()
        name = _clean_name(_t(a) or a.get("title") or "")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        link = urljoin(base, href).split("#")[0] if href and not href.startswith(("#", "javascript:")) else None
        out.append({"name": name, "link": link})
    return out


def _plain(names):
    out, seen = [], set()
    for n in names:
        n = _clean_name(n)
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append({"name": n, "link": None})
    return out


def _count(v):
    m = re.match(r"^\s*([\d.,]+)\s*([kKmM]?)\s*$", v or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "k": 1000, "m": 1000000}[m.group(2).lower()])


def _site_metadata(soup, host, url, title):
    """-> dict with any of title / description / duration_seconds / date / views / rating / groups.
    A key that is present REPLACES the generic result (description None = the site has no story)."""
    site, g = {}, {}
    if host.endswith("freesexvideos.xxx"):
        for item in soup.select(".block-details .item"):
            label = _t(item.find("span")).rstrip(":").lower()
            links = [a for a in item.find_all("a", href=True) if (a["href"] or "").strip() not in ("", "#")]
            kind = {"channel": "studios", "network": "studios", "categories": "categories", "pornstars": "models"}.get(label)
            if kind and links:
                g.setdefault(kind, []).extend(_chips(links, url))
        tags = [x for v in _meta(soup, "video:tag") for x in re.split(r"[,;]", v)]
        if tags:
            g["tags"] = _plain(tags)
        site["description"] = None            # the page has no story, only a generic "Watch X on Free Sex Videos" line
        # views / votes of THIS video live in its own action bar (the generic scan would pick a related video's)
        site["views"] = site["rating"] = None
        m = re.match(r"^([\d.,]+\s*[kKmM]?)", _t(soup.select_one(".info-buttons .views")))
        if m:
            site["views"] = m.group(1).replace(" ", "")
        votes = [_count(_t(c)) for c in soup.select(".info-buttons .vote-wrapper .count")]
        if len(votes) == 2 and sum(votes) > 0:
            site["rating"] = f"{round(100 * votes[0] / sum(votes))}%"
        h = soup.select_one("#tab_video_info h1") or soup.find("h1")
        t = re.sub(r"\s*/\s*\d{1,2}\.\d{1,2}\.\d{4}\s*$", "", _t(h))
        known = {_norm(x["name"]) for v in g.values() for x in v if x.get("name")}
        parts = [p for p in t.split(" - ")]
        while len(parts) > 1 and _norm(parts[0]) in known:
            parts.pop(0)
        if t:
            site["title"] = " - ".join(parts).strip()
    elif "pornvideobb" in host:
        full = _t(soup.select_one("h1.block-name-porn"))
        rows = {}
        for row in soup.select(".category-spisok"):
            lab = _t(row.select_one(".cat-zagolovok")).rstrip(":").lower()
            rows[lab] = _chips(row.find_all("a", href=True), url)
        for lab, kind in (("categories", "categories"), ("porn star", "models"), ("pornstar", "models"), ("studio", "studios")):
            if rows.get(lab):
                g.setdefault(kind, []).extend(rows[lab])
        # "Tags:" only repeats the genres with synonyms (beautiful / beauties / ass / booty ...): not shown
        for li in soup.select(".porn-info li"):
            tx = _t(li)
            m = re.match(r"Views:\s*([\d.,]+\s*[kKmM]?)", tx)
            if m:
                site["views"] = m.group(1).replace(" ", "")
            m = re.match(r"Date:\s*(\d{4}-\d{2}-\d{2})", tx)
            if m:
                site["date"] = m.group(1)
        if full:
            t = re.sub(r"^porn video\s+", "", full, flags=re.I)
            names = [x["name"] for k in ("models", "studios") for x in g.get(k, [])]
            changed = True
            while changed:                      # the page appends "<stars> <studios>" to the title: remove them
                changed, t = False, t.rstrip(" ,")
                for n in sorted(names, key=len, reverse=True):
                    if t.lower().endswith(n.lower()) and len(t) > len(n) + 3:
                        t, changed = t[:-len(n)].rstrip(" ,"), True
                        break
            site["title"] = t
        desc = _t(soup.select_one(".mini-description"))
        if full and desc.lower().startswith(full.lower()):
            desc = desc[len(full):].strip()   # the story block starts with the title + names again
        site["description"] = desc or None
    elif host.endswith("superporn.com"):
        player = soup.select_one("[data-video-duration]")
        if player and _to_seconds(player.get("data-video-duration")):
            site["duration_seconds"] = _to_seconds(player.get("data-video-duration"))
        nv = soup.select_one("#n-views")
        if nv and _t(nv):
            site["views"] = _t(nv)
        sub = _t(soup.select_one(".data-video .subido")).strip(" ·")
        if sub:
            site["date"] = sub
        like = dislike = None
        for a in soup.select(".data-video a"):
            tx = _t(a)
            m = re.match(r"^([\d.,]+\s*[kKmM]?)\s+I like it", tx)
            if m:
                like = _count(m.group(1))
            m = re.match(r"^([\d.,]+\s*[kKmM]?)\s+I don'?t like it", tx)
            if m:
                dislike = _count(m.group(1))
        if like is not None and (like + (dislike or 0)) > 0:
            site["rating"] = f"{round(100 * like / (like + (dislike or 0)))}%"
        # genre chips (folder icon + name, e.g. "Big tits", "Blowjob"): wherever the page puts them, but never in related-video cards
        chips = []
        for a in soup.select(".catlist a[href], .chip-group a[href], a.chip-link") + \
                [x for x in soup.find_all("a", href=True) if x.find("i", class_=re.compile(r"icon-folder"))]:
            if a in chips:
                continue
            if any(p.name not in ("body", "html") and re.search(r"(?:^|\s)(?:thumb-video|related|navbar|footer)", " ".join(p.get("class") or []))
                   for p in a.parents):
                continue                         # a card of another video / site chrome, not this video's genres
            chips.append(a)
        chips = chips[:40]
        stars = [a for a in chips if re.search(r"/(?:pornstars?|models?|stars?)/", a.get("href") or "", re.I)]
        cats = [a for a in chips if a not in stars]
        if stars:
            g["models"] = _chips(stars, url)
        series = [a for a in soup.select(".data-video a[href*='/series/']") if _t(a)]
        if series:
            g["studios"] = _chips(series[:1], url)
        up = soup.select(".data-video a.info-uploader")
        if up:
            g["uploaders"] = _chips(up[:1], url)
        if cats:
            g["categories"] = _chips(cats, url)
        desc = _t(soup.select_one("#resume"))
        site["description"] = None if (not desc or _norm(desc) == _norm(title)) else desc
    elif host.endswith("bdsmhole.com"):
        def mi(name):
            tag = soup.find("meta", attrs={"itemprop": name})
            return (tag.get("content") or "").strip() if tag else ""
        cands = [_t(e) for e in soup.select(".product_desc")] + [mi("description")]
        cands = [c for c in cands if c and not c.lower().startswith("pornstars")]
        if cands:
            site["description"] = max(cands, key=len)      # the full story, not the one-sentence meta description
        m = re.search(r"(\d[\d,]*)", mi("interactionCount").replace(" ", ""))
        if m:
            site["views"] = f"{int(m.group(1).replace(',', '')):,}"
        try:
            rv, best = float(mi("ratingValue")), float(mi("bestRating") or 5)
            if rv > 0:
                site["rating"] = f"{round(100 * rv / best)}%"
        except ValueError:
            pass
        if re.match(r"\d{4}-\d{2}-\d{2}", mi("uploadDate")):
            site["date"] = mi("uploadDate")[:10]
        if _to_seconds(mi("duration")):
            site["duration_seconds"] = _to_seconds(mi("duration"))
        for dl in soup.select(".datalist"):
            label = _t(dl.select_one(".datalist_title")).lower()
            links = dl.select(".datalist_content a[href]")
            kind = {"channel": "studios", "pornstars": "models", "tags": "tags"}.get(label)
            if kind and links:
                g[kind] = _chips(links, url)
        genres = [x for v in _meta(soup, "video:tag") for x in re.split(r"[,;]", v)]
        if genres:
            g["categories"] = _plain(genres)    # the site's own genre list; the page's Tags row is separate
    else:
        return {}
    if g:
        site["groups"] = g
    return site


def _dedupe_groups(groups):
    """No name twice: inside a group, and tags/genres never repeat a star, studio, uploader or each other."""
    for k in list(groups):
        seen, keep = set(), []
        for x in groups[k]:
            key = x["name"].lower()
            if key not in seen:
                seen.add(key)
                keep.append(x)
        groups[k] = keep
    people = {x["name"].lower() for k in ("models", "studios", "uploaders") for x in groups.get(k, [])}
    if groups.get("categories"):
        groups["categories"] = [x for x in groups["categories"] if x["name"].lower() not in people]
    cats = {x["name"].lower() for x in groups.get("categories", [])}
    if groups.get("tags"):
        groups["tags"] = [x for x in groups["tags"] if x["name"].lower() not in people | cats]
    return {k: v for k, v in groups.items() if v}


_EMB_LISTS = {
    "models": ("pornstars", "pornStars", "performers", "models", "actors", "stars", "cast"),
    "categories": ("categories", "genres", "niches", "category", "genre"),
    "tags": ("tags", "keywords"),
    "studios": ("channel", "studio", "network", "series", "site", "producer"),
}


def _norm_title(t):
    return re.sub(r"[\W_]+", " ", (t or "").lower()).strip()


def _embedded_video(soup, title):
    """Some SPA-style pages keep duration / stars / genres only in an embedded JSON state. Find the object of THIS
    video there (matched by title) and return it; never guesses from other videos."""
    want = _norm_title(title)
    if len(want) < 4:
        return None
    roots = []
    for sc in soup.find_all("script"):
        if "ld+json" in (sc.get("type") or "").lower():
            continue
        raw = (sc.string or sc.get_text() or "").strip()
        if len(raw) < 40 or len(raw) > 3_000_000:
            continue
        txt = raw if raw[0] in "[{" else None
        if txt is None:
            m = re.search(r"(?:__[A-Z0-9_]+__|INITIAL_STATE|initialState)\s*=\s*([\[{].*[\]}])\s*;?\s*$", raw, re.S)
            txt = m.group(1) if m else None
        if not txt:
            continue
        try:
            roots.append(json.loads(txt))
        except ValueError:
            pass
    stack, n = list(roots), 0
    while stack and n < 30000:
        n += 1
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(v for v in x if isinstance(v, (dict, list)))
            continue
        if not isinstance(x, dict):
            continue
        t = x.get("title") if x.get("title") is not None else x.get("name")
        if isinstance(t, str):
            tn = _norm_title(t)
            if tn == want or (len(tn) > 8 and (tn in want or want in tn)):
                return x
        stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return None


def _emb_names(v):
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[,;|]", v) if p.strip()]
    if isinstance(v, list):
        return [n for x in v for n in _emb_names(x if isinstance(x, (str, dict)) else str(x))]
    if isinstance(v, dict):
        return _emb_names(v.get("name") or v.get("title") or v.get("slug") or "")
    return []


def extract_metadata(html, url):
    soup = BeautifulSoup(html, "lxml")
    ld = _json_ld_video_only(soup)
    out = {"url": url, "title": None, "duration": None, "duration_seconds": None, "date": None,
           "views": None, "rating": None, "description": None, "groups": []}

    h1 = _pick_h1(soup)
    out["title"] = (ld.get("name") if isinstance(ld.get("name"), str) else None) \
        or (h1.get_text(" ", strip=True) if h1 else None) \
        or (_meta(soup, "og:title") or [None])[0]

    if out["title"]:
        out["title"] = re.sub(r"\s+", " ", _unescape_text(out["title"])).strip() or None

    sec = _to_seconds(ld.get("duration"))
    for cand in (_meta(soup, "video:duration", "og:video:duration", "duration") if not sec else []):
        sec = sec or _to_seconds(cand)
    if not sec:
        for el in soup.find_all(attrs={"itemprop": "duration"}):
            sec = _to_seconds(el.get("content") or el.get_text(strip=True))
            if sec:
                break
    if not sec:
        text = (h1.parent.get_text(" ", strip=True) if h1 and h1.parent else soup.get_text(" ", strip=True))[:4000]
        m = re.search(r"(?:duration|length|runtime|время|длительность)\s*[:\-]?\s*((?:\d{1,2}:)?\d{1,2}:\d{2})", text, re.I)
        sec = _to_seconds(m.group(1)) if m else None
    if sec:
        out["duration_seconds"], out["duration"] = sec, _fmt_seconds(sec)

    for cand in [ld.get("uploadDate"), ld.get("datePublished")] + _meta(soup, "video:release_date", "article:published_time", "uploadDate", "datePublished"):
        if isinstance(cand, str) and re.match(r"\d{4}-\d{2}-\d{2}", cand):
            out["date"] = cand[:10]
            break
    if not out["date"]:
        t = soup.find("time", attrs={"datetime": True})
        if t and re.match(r"\d{4}-\d{2}-\d{2}", t["datetime"]):
            out["date"] = t["datetime"][:10]

    stat = ld.get("interactionStatistic")
    for s in (stat if isinstance(stat, list) else [stat] if stat else []):
        if isinstance(s, dict) and s.get("userInteractionCount") is not None:
            if re.search(r"Watch|View", json.dumps(s.get("interactionType", "")), re.I) or len(stat if isinstance(stat, list) else [stat]) == 1:
                out["views"] = _parse_views(s["userInteractionCount"])
                break
    if not out["views"] and h1 is not None:
        for el in h1.find_all_next(True, class_=re.compile(r"(views?|eye)", re.I), limit=60):
            t = el.get_text(" ", strip=True)
            if re.fullmatch(r"\d[\d\s,.]*\s?[kKmM]?", t or ""):
                out["views"] = t
                break

    ar = ld.get("aggregateRating")
    if isinstance(ar, dict) and ar.get("ratingValue") is not None:
        out["rating"] = str(ar["ratingValue"])

    d = ld.get("description") if isinstance(ld.get("description"), str) else (_meta(soup, "description", "og:description") or [None])[0]
    if d:
        out["description"] = re.sub(r"\s+", " ", _unescape_text(d)).strip()[:6000]

    # Labeled rows ("Categories:", "Tags:", "Porn star:", "Studio:") are trusted when present;
    # otherwise fall back to recognising links by their URL shape.
    labeled = _labeled_groups(soup, url)
    linked = {} if labeled else _linked_groups(soup, url)
    groups = {k: list(v) for k, v in (labeled or linked).items()}

    def add_names(kind, names):
        have = {x["name"].lower() for x in groups.get(kind, [])}
        for n in names:
            n = _clean_name(n)
            if not n or n.lower() in have:
                continue
            have.add(n.lower())
            entry = {"name": n, "link": None}
            # the page usually links the same name somewhere else (e.g. a flat tag row):
            # adopt that link and move it out of the generic "tags" group
            tags = groups.get("tags") or []
            hit = next((t for t in tags if t["name"].lower() == n.lower() and t.get("link")), None)
            if hit is not None and kind != "tags":
                entry = hit
                tags.remove(hit)
                if not tags:
                    groups.pop("tags", None)
            groups.setdefault(kind, []).append(entry)

    if not labeled and not groups.get("tags") and not groups.get("categories"):
        flat = _flat_tags(soup, url, h1)
        if flat:
            groups["tags"] = flat
    add_names("categories", _names(ld.get("genre")) + _meta(soup, "article:section"))
    add_names("models", _names(ld.get("actor")) + _names(ld.get("performer")))
    add_names("studios", _names(ld.get("productionCompany")) + _names(ld.get("publisher")))
    if not groups.get("tags"):
        add_names("tags", _names(ld.get("keywords")) + _meta(soup, "video:tag", "article:tag"))
    if not groups.get("tags") and not groups.get("categories"):
        add_names("tags", [x for k in _meta(soup, "keywords") for x in _names(k)][:20])

    # the four built-in sites: exact, site-specific extraction replaces the generic guess
    host = (urlparse(url).hostname or "").lower()
    site = _site_metadata(soup, host, url, out["title"])
    for k in ("title", "description", "date", "views", "rating"):
        if k in site:
            out[k] = site[k]
    if site.get("duration_seconds"):
        out["duration_seconds"], out["duration"] = site["duration_seconds"], _fmt_seconds(site["duration_seconds"])
    if site.get("groups"):
        groups = {k: list(v) for k, v in site["groups"].items()}

    # fill what the HTML did not give from the page's embedded JSON state (only this video's own object)
    emb = None if site else _embedded_video(soup, out["title"])
    if emb:
        def pick(*keys):
            for k in keys:
                if emb.get(k) not in (None, ""):
                    return emb[k]
            return None
        if not out["duration_seconds"]:
            s = _to_seconds(pick("duration", "durationSeconds", "duration_seconds", "length", "lengthSeconds", "runtime"))
            if s:
                out["duration_seconds"], out["duration"] = s, _fmt_seconds(s)
        if not out["views"]:
            v = pick("views", "viewCount", "view_count", "viewsCount", "numViews")
            if v is not None and not isinstance(v, (dict, list)):
                out["views"] = f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else (str(v).strip() or None)
        if not out["rating"]:
            r = pick("rating", "ratingValue", "likesPercent", "likes_percent")
            if r is not None and not isinstance(r, (dict, list)):
                out["rating"] = str(r)
        if not out["date"]:
            d = pick("uploadDate", "createdAt", "created_at", "publishedAt", "published_at", "datePublished", "releaseDate", "added")
            if isinstance(d, str) and re.match(r"\d{4}-\d{2}-\d{2}", d):
                out["date"] = d[:10]
        for kind, keys in _EMB_LISTS.items():
            if groups.get(kind):
                continue
            k = next((x for x in keys if emb.get(x) not in (None, "")), None)
            if k:
                add_names(kind, _emb_names(emb[k])[:30])

    # PornVideoBB shows the same words as "genre" and "tags": keep one list (Genres). Elsewhere, drop tags that repeat a genre.
    if "pornvideobb" in host and not site.get("groups"):     # unknown layout: its Tags only repeat the Genres
        groups.pop("tags", None)
    groups = _dedupe_groups(groups)

    out["groups"] = [{"kind": k, "label": _LABELS[k], "items": groups[k][:40]} for k in _ORDER if groups.get(k)]
    return out


def fetch_metadata(html, url):
    try:
        return extract_metadata(html, url)
    except Exception:
        return {"url": url, "error": "metadata extraction failed", "groups": []}