"""Search everything: match a keyword against title, tags, genres/categories, stars, studios, description and URL slug.

Python twin of src/search.ts - the two MUST rank the same way, because scraper_kv.py pre-ranks cached results and the
Worker re-ranks them together with the search index.  Used by scraper_kv.py (cache + index builder) and app.py.
"""
import re
import unicodedata
from urllib.parse import unquote, urlparse

_STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "with", "for", "to", "video", "videos", "porn", "free", "hd",
         "xxx", "full", "movie", "movies", "sex", "online"}
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_GROUP_WEIGHT = {"categories": 1, "models": 1, "tags": 0, "studios": -1, "via": -1}
_FIELD_ORDER = ("categories", "tags", "models", "studios", "via")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return _NON_WORD.sub(" ", s).strip()


def stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and re.search(r"(ches|shes|sses|xes|zes)$", w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not w.endswith("us"):
        return w[:-1]
    return w


def stems(s):
    return [stem(w) for w in norm(s).split() if w]


def query_key(q):
    """Cache-key form of a query. MUST equal queryKey() in src/search.ts."""
    return " ".join((q or "").strip().lower().split())


def parse_query(q):
    allw = norm(q).split()
    keep = [w for w in allw if w not in _STOP]
    words = (keep or allw)[:8]
    return {"raw": (q or "").strip(), "norm": " ".join(words), "stems": [stem(w) for w in words], "slug": "-".join(allw)}


def _word_score(words, st, exact, prefix):
    best = 0
    for w in words:
        if w == st:
            return exact
        if best < prefix and len(w) > len(st) and len(st) >= 3 and w.startswith(st):
            best = prefix
    return best


def _slug_words(link):
    if not link:
        return []
    try:
        return stems(re.sub(r"\.(html?|php|aspx?)$", "", unquote(urlparse(link).path), flags=re.I))
    except Exception:
        return []


def groups_of(it):
    def arr(v):
        if isinstance(v, list):
            return [x if isinstance(x, str) else (x or {}).get("name") for x in v if x]
        if isinstance(v, str):
            return [x.strip() for x in re.split(r"[,;|]", v) if x.strip()]
        return []
    return {"tags": arr(it.get("tags")), "categories": arr(it.get("categories") or it.get("genres")),
            "models": arr(it.get("models") or it.get("stars") or it.get("pornstars")),
            "studios": arr(it.get("studios") or it.get("series")), "via": arr(it.get("_via"))}


def score_item(it, q):
    """-> (score, coverage, matches). Same weights as scoreSearchable() in search.ts."""
    if not q["stems"]:
        return 0, 0.0, []
    title = stems(it.get("title"))
    title_n = norm(it.get("title"))
    desc = stems((it.get("description") or "")[:600])
    slug = _slug_words(it.get("link"))
    g = groups_of(it)
    fields = []
    for kind in _FIELD_ORDER:
        for v in g.get(kind) or []:
            n = norm(v)
            if n:
                fields.append((kind, n, [stem(w) for w in n.split()]))
    total, matched, matches = 0, 0, []
    for st in q["stems"]:
        best, label = 0, ""
        t = _word_score(title, st, 12, 9)
        if t > best:
            best, label = t, "title"
        for kind, _n, w in fields:
            base = 11 if (len(w) == 1 and w[0] == st) else _word_score(w, st, 8, 6)
            if not base:
                continue
            s = base + _GROUP_WEIGHT[kind]
            if s > best:
                best, label = s, kind
        d = _word_score(desc, st, 3, 2)
        if d > best:
            best, label = d, "description"
        sl = _word_score(slug, st, 3, 2)
        if sl > best:
            best, label = sl, "url"
        if best > 0:
            matched += 1
            total += best
            if label not in matches:
                matches.append(label)
    coverage = matched / len(q["stems"])
    if len(q["stems"]) > 1:
        if q["norm"] in title_n:
            total += 8
            if "title" not in matches:
                matches.append("title")
        for kind, n, _w in fields:
            if n == q["norm"]:
                total += 10
                if kind not in matches:
                    matches.append(kind)
                break
    return total, coverage, matches


def _quality(it):
    return (2 if it.get("thumbnail") else 0) + (1 if it.get("duration") else 0) + (1 if it.get("views") else 0) + (1 if it.get("rating") else 0)


def relevance(it, q, trusted=False):
    """None = not relevant. `trusted`: the site's own search returned it, so it stays even when no field shows the keyword."""
    score, cov, matches = score_item(it, q)
    need = 0.5 if len(q["stems"]) > 1 else 1.0
    if not trusted and (cov < need or score <= 0):
        return None
    s = score * (cov * cov if cov < 1 else 1) + _quality(it)
    if trusted:
        s += 8
    return s, matches


def _no_query(u):
    return (u or "").split("?")[0]


def rank_combined(results, q, index_hits=(), cap=300):
    """Merge per-site results (+ index hits) into one list, de-duplicated by link, ranked by relevance."""
    seen = {}

    def put(it, site, trusted, via):
        k = _no_query(it.get("link"))
        if not k:
            return
        r = relevance(it, q, trusted)
        if not r:
            return
        prev = seen.get(k)
        if prev and prev["_score"] >= r[0]:
            if via not in prev["_matches"]:
                prev["_matches"].append(via)
            return
        o = {**it, "_site": site, "_score": round(r[0], 1), "_match": r[1], "_matches": [via]}
        if prev:
            for gk in ("tags", "categories", "models", "studios"):
                if not o.get(gk) and prev.get(gk):
                    o[gk] = prev[gk]
        seen[k] = o

    for res in results:
        site = res.get("site") or res.get("page") or ""
        via = "taxonomy:" + str(res.get("via") or "") if res.get("source") == "taxonomy" else "site"
        for it in res.get("items") or []:
            put(it, site, True, via)
    for it in index_hits:
        put(it, it.get("page") or "index", False, "index")
    out = []
    for o in seen.values():
        m = o.pop("_matches")
        o["_via"] = ",".join(m)
        out.append(o)
    out.sort(key=lambda x: -x["_score"])
    return out[:cap]


# ----------------------------------------------------------------------------- index + taxonomy records (written by scraper_kv)
def reg_host(url):
    h = (urlparse(url if "://" in (url or "") else "https://" + (url or "")).hostname or "").lower()
    h = re.sub(r"^www\d*\.", "", h)
    p = h.split(".")
    if len(p) <= 2:
        return h
    if p[-2] in ("co", "com", "org", "net", "gov", "ac") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _clip(lst, n, each=60):
    out, seen = [], set()
    for x in lst or []:
        x = (x if isinstance(x, str) else (x or {}).get("name") or "").strip()
        if x and len(x) <= each and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
        if len(out) >= n:
            break
    return out


def index_record(item, meta=None, via=None, ts=0):
    """Compact KV record (short keys keep the single `search-index` key small) from a scraped item + its metadata."""
    groups = {}
    for gd in (meta or {}).get("groups") or []:
        groups[gd.get("kind")] = [x.get("name") for x in gd.get("items") or [] if isinstance(x, dict)]
    rec = {"l": item.get("link"), "t": (item.get("title") or "")[:160], "i": item.get("thumbnail"),
           "du": (meta or {}).get("duration") or item.get("duration"), "vw": (meta or {}).get("views") or item.get("views"),
           "rt": (meta or {}).get("rating") or item.get("rating"), "ad": (meta or {}).get("date") or item.get("added"),
           "ql": item.get("quality"), "p": item.get("page"),
           "tg": _clip(groups.get("tags"), 14) or _clip(item.get("tags"), 14),
           "ct": _clip(groups.get("categories"), 8) or _clip(item.get("categories"), 8),
           "md": _clip(groups.get("models"), 8), "st": _clip((groups.get("studios") or []) + (groups.get("uploaders") or []), 4),
           "vi": _clip(via, 6), "ds": ((meta or {}).get("description") or "")[:220] or None, "ts": ts}
    return {k: v for k, v in rec.items() if v not in (None, "", [])}
