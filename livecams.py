"""Live Cams: a grid of live-stream cards, each with thumbnail, viewers, country, tags and a link to the stream.

Cards come from two kinds of sources, merged and de-duplicated by (platform, username):

  1. DIRECT platform feeds (the main source): the public room-list JSON of Chaturbate, Stripchat, Cam4 and
     CamSoda.  They are fetched in parallel, each one independent: if one platform blocks the server,
     the others still fill the page.  Every card links straight to the stream's room on the platform.
  2. LEMONCAMS (bonus): lemoncams.com is an Angular app, so the HTML its server sends is an EMPTY shell and
     its cards are drawn by JavaScript from a JSON API.  It is tried in the background (API discovery /
     LEMONCAMS_API_URL / headless browser, see _fetch_lemoncams) and merged in whenever it succeeds.

Card fields: username, age, provider, provider_name, link, thumbnail, viewers, country, country_code,
flag_emoji, gender, languages, room_title, categories, hd, is_new, id.   (No tags: the only filters are the
Lemoncams CATEGORIES and COUNTRIES, see fetch_lemon_filters.)

Environment variables (all optional):
    CAM_PROVIDERS        comma list of direct platforms to use, default "chaturbate,stripchat,cam4,camsoda"
    CHATURBATE_WM        Chaturbate affiliate campaign slug (only used by the fallback endpoint)
    LEMONCAMS_API_URL    JSON URL(s) copied from the browser's Network tab on lemoncams.com (the cams)
    LEMONCAMS_CATEGORIES_API / LEMONCAMS_COUNTRIES_API   same, for the category / country lists (optional)
Every step reports what happened in `diagnostics`, so a failure is explained instead of showing an empty page.
"""
import html as _html
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

HOME_URL = "https://www.lemoncams.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

_NUM = re.compile(r"\d[\d.,]*")


# ----------------------------------------------------------------------------- small helpers
def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el is not None else ""


def _abs(base, u):
    u = (u or "").strip()
    if not u or u.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    return urljoin(base, u).split("#")[0]


def _int(text):
    m = _NUM.search(text or "")
    if not m:
        return None
    try:
        return int(re.sub(r"[.,]", "", m.group(0)))
    except ValueError:
        return None


def _img_url(img, base):
    """Real URL of an <img>: live pages use src, a saved copy keeps it in data-savepage-*."""
    if img is None:
        return None
    for attr in ("src", "data-src", "data-savepage-currentsrc", "data-savepage-src", "data-original", "data-lazy-src"):
        v = (img.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return urljoin(base, v)
    ss = (img.get("srcset") or img.get("data-srcset") or "").split(",")[0].strip().split(" ")[0]
    return urljoin(base, ss) if ss and not ss.startswith("data:") else None


def _bg_url(el, base):
    """CSS background image of a banner (live: url("..."), saved copy: /*savepage-url=..*/)."""
    style = (el.get("style") or "") if el is not None else ""
    m = re.search(r"savepage-url=([^*]+?)\*/", style) or re.search(r"url\(\s*['\"]?([^'\")]+)", style)
    if not m or m.group(1).startswith("data:"):
        return None
    path = re.sub(r"^(?:\.\./|\./)+", "", m.group(1).strip())      # Angular component-relative paths
    return urljoin(base, path)


# ----------------------------------------------------------------------------- one cam card
_FOOTER_RX = re.compile(r"(?:^|\s)(username|id|title|tags|categories|haircolor|body|languages|"
                        r"lemoncams score|banned countries):\s*")


def _parse_footer_title(text):
    """The card footer's tooltip: 'username: x id: 1 title: ... tags: a,b categories: c ...'."""
    parts = _FOOTER_RX.split(text or "")
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def _csv(v, limit=12):
    return [x.strip() for x in re.split(r"[,;]", v or "") if x.strip() and "=" not in x][:limit]


def parse_cam(card, base):
    link_el = card.select_one("a[href]")
    link = _abs(base, link_el.get("href")) if link_el else None
    name_el = card.select_one(".footer-username") or card.select_one("a.footer-username")
    raw_name = _txt(name_el)
    age = None
    m = re.search(r"\((\d{2})\)\s*$", raw_name)
    if m:
        age, raw_name = int(m.group(1)), raw_name[:m.start()].strip()
    if not raw_name:
        thumb_alt = card.select_one("img.ratio_4-3_img")
        raw_name = (thumb_alt.get("alt") or "").strip() if thumb_alt else ""
    if not raw_name and not link:
        return None

    provider = None
    if link:
        segs = [s for s in urlparse(link).path.split("/") if s]
        if len(segs) >= 2:
            provider = segs[0]
    logo_img = card.select_one(".bottom-label img")
    logo = _img_url(logo_img, base)
    if not provider and logo_img is not None:
        provider = re.sub(r"\s*logo\s*$", "", logo_img.get("alt") or "", flags=re.I).strip() or None

    thumb = None
    for img in card.select("figure img"):
        if img.find_parent(class_="bottom-label") is None:           # not the provider logo
            thumb = _img_url(img, base)
            if thumb:
                break

    viewers = _int(_txt(card.select_one(".footer-left")))
    flag = card.select_one("img.flag-image")
    flag_src = _img_url(flag, base)
    country = None
    if flag_src and "/flags/" in flag_src:
        alt = (flag.get("alt") or "").strip()
        country = alt if alt and alt.lower() != "undefined" and len(alt) <= 40 else \
            re.sub(r"\.\w+$", "", flag_src.rsplit("/", 1)[-1]).upper()
    location = _txt(card.select_one(".footer-location-info"))

    footer = card.select_one("footer")
    meta = _parse_footer_title(footer.get("title") if footer is not None else "")
    categories = _csv(meta.get("categories"))
    if not categories and location and re.fullmatch(r"[\w/ &-]+(,[\w/ &-]+)*", location) and location != country:
        categories = _csv(location)

    return {
        "username": raw_name, "age": age, "provider": provider, "provider_logo": logo, "link": link,
        "thumbnail": thumb, "viewers": viewers, "country": country, "flag": flag_src if country else None,
        "room_title": (meta.get("title") or "").strip() or None,
        "categories": categories, "tags": [], "id": (meta.get("id") or "").strip() or None,
        "online": card.select_one(".status-span-online") is not None or None,
    }


# ----------------------------------------------------------------------------- the page: cam cards only
def parse_home(html, base=HOME_URL):
    """Rendered Lemoncams page -> {items: [cam, ...]}.  Only the live-stream cards: the site header, footer,
    sidebar, banners and blog are never read."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    for t in soup.select("app-navigation, app-footer, footer#footer, footer.footer, header.header, #header-mobile"):
        t.decompose()
    root = soup.find("app-home") or soup.select_one(".site-content") or soup.body or soup
    items, seen = [], set()
    for el in root.select(".posts__item--card"):
        cam = parse_cam(el, base)
        key = (cam or {}).get("link") or (cam or {}).get("username")
        if cam and key and key not in seen:
            seen.add(key)
            items.append(cam)
    return {"items": items}


# ----------------------------------------------------------------------------- platforms
_ROOM_URL = {                       # where a cam's live room lives on its own platform
    "chaturbate": "https://chaturbate.com/{u}/",
    "stripchat": "https://stripchat.com/{u}",
    "cam4": "https://www.cam4.com/{u}",
    "camsoda": "https://www.camsoda.com/{u}",
}
_PROVIDER_LABEL = {"chaturbate": "Chaturbate", "stripchat": "Stripchat", "cam4": "Cam4", "camsoda": "CamSoda",
                   "bongacams": "BongaCams", "myfreecams": "MyFreeCams", "livejasmin": "LiveJasmin",
                   "streamate": "Streamate", "flirt4free": "Flirt4Free"}
_COUNTRIES = {
    "US": "United States", "GB": "United Kingdom", "UK": "United Kingdom", "CA": "Canada", "MX": "Mexico",
    "CO": "Colombia", "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "PE": "Peru", "VE": "Venezuela",
    "EC": "Ecuador", "UY": "Uruguay", "DO": "Dominican Republic", "CU": "Cuba", "ES": "Spain", "PT": "Portugal",
    "FR": "France", "DE": "Germany", "IT": "Italy", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland",
    "AT": "Austria", "PL": "Poland", "CZ": "Czechia", "SK": "Slovakia", "HU": "Hungary", "RO": "Romania",
    "BG": "Bulgaria", "RS": "Serbia", "HR": "Croatia", "GR": "Greece", "TR": "Turkey", "UA": "Ukraine",
    "RU": "Russia", "BY": "Belarus", "MD": "Moldova", "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IE": "Ireland", "AU": "Australia",
    "NZ": "New Zealand", "JP": "Japan", "KR": "South Korea", "CN": "China", "TH": "Thailand", "PH": "Philippines",
    "ID": "Indonesia", "VN": "Vietnam", "IN": "India", "ZA": "South Africa", "KZ": "Kazakhstan",
}
_MORE_COUNTRIES = (
    "AF:Afghanistan|AL:Albania|DZ:Algeria|AD:Andorra|AO:Angola|AM:Armenia|AZ:Azerbaijan|BS:Bahamas|BH:Bahrain|"
    "BD:Bangladesh|BB:Barbados|BZ:Belize|BJ:Benin|BT:Bhutan|BO:Bolivia|BA:Bosnia and Herzegovina|BW:Botswana|"
    "BN:Brunei|BF:Burkina Faso|BI:Burundi|KH:Cambodia|CM:Cameroon|CV:Cape Verde|CF:Central African Republic|"
    "TD:Chad|CR:Costa Rica|CI:Ivory Coast|CY:Cyprus|CD:DR Congo|CG:Congo|DJ:Djibouti|EG:Egypt|SV:El Salvador|"
    "ER:Eritrea|ET:Ethiopia|FJ:Fiji|GA:Gabon|GM:Gambia|GE:Georgia|GH:Ghana|GT:Guatemala|GN:Guinea|GY:Guyana|"
    "HT:Haiti|HN:Honduras|HK:Hong Kong|IS:Iceland|IR:Iran|IQ:Iraq|IL:Israel|JM:Jamaica|JO:Jordan|KE:Kenya|"
    "KW:Kuwait|KG:Kyrgyzstan|LA:Laos|LB:Lebanon|LS:Lesotho|LR:Liberia|LY:Libya|LI:Liechtenstein|LU:Luxembourg|"
    "MO:Macao|MK:North Macedonia|MG:Madagascar|MW:Malawi|MY:Malaysia|MV:Maldives|ML:Mali|MT:Malta|MR:Mauritania|"
    "MU:Mauritius|MC:Monaco|MN:Mongolia|ME:Montenegro|MA:Morocco|MZ:Mozambique|MM:Myanmar|NA:Namibia|NP:Nepal|"
    "NI:Nicaragua|NE:Niger|NG:Nigeria|OM:Oman|PK:Pakistan|PS:Palestine|PA:Panama|PG:Papua New Guinea|PY:Paraguay|"
    "PR:Puerto Rico|QA:Qatar|RW:Rwanda|SA:Saudi Arabia|SN:Senegal|SG:Singapore|SI:Slovenia|SO:Somalia|"
    "LK:Sri Lanka|SD:Sudan|SR:Suriname|SZ:Eswatini|SY:Syria|TW:Taiwan|TJ:Tajikistan|TZ:Tanzania|TG:Togo|"
    "TT:Trinidad and Tobago|TN:Tunisia|TM:Turkmenistan|UG:Uganda|AE:United Arab Emirates|UZ:Uzbekistan|"
    "VA:Vatican City|YE:Yemen|ZM:Zambia|ZW:Zimbabwe|XK:Kosovo|AW:Aruba|CW:Curacao|GU:Guam|KY:Cayman Islands|"
    "BM:Bermuda|GI:Gibraltar|JE:Jersey|IM:Isle of Man|RE:Reunion|MQ:Martinique|GP:Guadeloupe|PF:French Polynesia|"
    "NC:New Caledonia|AG:Antigua and Barbuda|DM:Dominica|GD:Grenada|LC:Saint Lucia|KN:Saint Kitts and Nevis|"
    "VC:Saint Vincent and the Grenadines|SC:Seychelles|SL:Sierra Leone|SS:South Sudan|ST:Sao Tome and Principe|"
    "TL:East Timor|TO:Tonga|WS:Samoa|VU:Vanuatu|SB:Solomon Islands|KI:Kiribati|NR:Nauru|PW:Palau|MH:Marshall Islands|"
    "FM:Micronesia|TV:Tuvalu|GQ:Equatorial Guinea|GW:Guinea-Bissau|KM:Comoros|BY:Belarus|SM:San Marino|FO:Faroe Islands|"
    "GL:Greenland|AX:Aland Islands|MD:Moldova|AN:Netherlands Antilles|VI:US Virgin Islands|VG:British Virgin Islands")
for _pair in _MORE_COUNTRIES.split("|"):
    _c, _n = _pair.split(":", 1)
    _COUNTRIES.setdefault(_c, _n)
_NAME2CODE = {v.lower(): k for k, v in _COUNTRIES.items() if k != "UK"}
_NAME2CODE.update({"usa": "US", "united states of america": "US", "uk": "GB", "great britain": "GB", "england": "GB",
                   "czech republic": "CZ", "korea": "KR", "russian federation": "RU", "vietnam": "VN",
                   "turkiye": "TR", "holland": "NL", "burma": "MM", "macedonia": "MK", "cote d'ivoire": "CI"})

_K_NAME = ("username", "user_name", "userName", "nickname", "nick", "displayName", "display_name", "model",
           "modelName", "name", "slug")
_K_PROVIDER = ("provider", "providerName", "provider_name", "site", "siteName", "source", "network", "platform")
_K_VIEWERS = ("num_users", "viewersCount", "viewers", "viewerCount", "viewer_count", "users", "numUsers",
              "num_viewers", "connections", "online")
_K_THUMB = ("previewUrlThumbBig", "image_url_360x270", "img", "thumbnail", "thumbnailUrl", "thumbnail_url",
            "snapshotUrl", "previewUrl", "previewImage", "image_url", "thumb", "thumbUrl", "image", "imageUrl",
            "preview", "snapshot", "screenshot", "poster", "picture", "photo", "profileImage", "previewUrlThumbSmall",
            "avatarUrl")
_K_COUNTRY = ("country", "countryName", "country_name", "countryCode", "country_code", "nation")
_K_TITLE = ("room_subject", "subject", "topic", "title", "roomTitle", "room_title", "headline", "statusMessage",
            "subject_html")
_K_LINK = ("link", "url", "profileUrl", "profile_url", "href", "permalink")
_NOT_PUBLIC = {"private", "hidden", "group", "groupshow", "group_show", "offline", "away", "ticketshow", "spy",
               "p2p", "password"}


def _add_thumb(lst, u):
    """Append a usable thumbnail URL (https, no duplicates, no inline data)."""
    if not u or u.startswith("data:"):
        return
    if u.startswith("http://"):
        u = "https://" + u[7:]
    if u not in lst:
        lst.append(u)


def _default_thumbs(provider, name):
    """Predictable snapshot URLs, used as fallbacks when the feed's own thumbnail is missing or broken."""
    if provider == "chaturbate" and name:
        n = quote(name)
        return [f"https://thumb.live.mmcdn.com/riw/{n}.jpg", f"https://roomimg.stream.highwebmedia.com/ri/{n}.jpg",
                f"https://thumb.live.mmcdn.com/ri/{n}.jpg"]
    if provider == "cam4" and name:
        return [f"https://snapshots.xcdnpro.com/thumbnails/{quote(name)}"]
    return []


def _first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _as_url(v, base):
    if isinstance(v, dict):
        v = _first(v, ("url", "src", "href", "large", "medium", "small", "default"))
    if isinstance(v, list) and v:
        return _as_url(v[0], base)
    if isinstance(v, str) and v.strip() and (v.startswith(("http://", "https://", "//", "/")) or "." in v):
        v = v.strip()
        return "https:" + v if v.startswith("//") else urljoin(base, v)
    return None


def _clean(s, limit=200):
    s = _html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _flag(code):
    code = (code or "").upper()
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code) if re.fullmatch(r"[A-Z]{2}", code) else None


def _country(v):
    """-> (display name, ISO code|None) from 'US', 'Colombia', {'name':..} ..."""
    if isinstance(v, dict):
        v = _first(v, ("name", "code"))
    v = str(v or "").strip()
    if not v or v.lower() in ("undefined", "none", "null", "unknown"):
        return None, None
    if re.fullmatch(r"[A-Za-z]{2}", v):
        code = v.upper()
        return _COUNTRIES.get(code, code), ("GB" if code == "UK" else code)
    return v[:40], _NAME2CODE.get(v.lower())


def _names(v, limit=14):
    """['a', {'name': 'b'}] or 'a, b' -> ['a', 'b']"""
    if isinstance(v, str):
        return _csv(v, limit)
    out = []
    for x in v if isinstance(v, list) else []:
        s = x.get("name") or x.get("slug") if isinstance(x, dict) else x
        s = str(s or "").strip().lstrip("#")
        if s and s not in out:
            out.append(s)
    return out[:limit]


def _to_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return _int(str(v or ""))


def _cam_from_dict(d, base, provider=None, need_thumb=True):
    """One room object of any platform's JSON -> a card (None when it is not a public, usable cam)."""
    name = _first(d, _K_NAME)
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    thumbs = []
    for k in _K_THUMB:
        if k in d:
            _add_thumb(thumbs, _as_url(d[k], base))
    if provider == "stripchat" and d.get("id") is not None and d.get("snapshotTimestamp"):
        _add_thumb(thumbs, f"https://img.doppiocdn.com/thumbs/{d['snapshotTimestamp']}/{d['id']}")
    for u in _default_thumbs(provider, name):
        _add_thumb(thumbs, u)
    thumb = thumbs[0] if thumbs else None
    if not thumb and need_thumb:
        return None
    show = str(_first(d, ("current_show", "showType", "status")) or "").strip().lower()
    if show in _NOT_PUBLIC:
        return None

    prov = provider
    if not prov:
        p = _first(d, _K_PROVIDER)
        if isinstance(p, dict):
            p = _first(p, ("name", "slug", "id"))
        prov = str(p).strip().lower() if p else None
    link = None
    if prov in _ROOM_URL:
        link = _ROOM_URL[prov].format(u=quote(name))
    if not link:
        link = _as_url(_first(d, _K_LINK), base)
    if not link and prov:
        link = urljoin(base, f"/{prov}/{name}")

    country, code = _country(_first(d, _K_COUNTRY))
    loc = _clean(d.get("location"), 60) if isinstance(d.get("location"), str) else ""
    if not country and loc:
        country, code = _country(loc)
        loc = ""
    age = d.get("display_age") or d.get("age")
    age = _to_int(age)
    cats = _names(d.get("categories") or d.get("category") or [])
    langs = d.get("spoken_languages") or d.get("languages") or d.get("language")
    gender = str(d.get("gender") or d.get("broadcastGender") or "").strip().lower() or None
    title = _clean(_first(d, _K_TITLE), 160) or None
    return {
        "username": name, "age": age if age and 18 <= age < 100 else None, "provider": prov,
        "provider_name": _PROVIDER_LABEL.get(prov or "", (prov or "").title() or None), "provider_logo": None,
        "link": link, "thumbnail": thumb, "thumbnails": thumbs[:5], "viewers": _to_int(_first(d, _K_VIEWERS)),
        "country": country, "country_code": code, "flag": None, "flag_emoji": _flag(code),
        "location": loc or None, "gender": {"f": "female", "m": "male", "t": "trans", "c": "couple"}.get(gender, gender),
        "languages": _names(langs, 6) if langs else [],
        "room_title": title, "categories": cats, "tags": [],
        "hd": bool(d.get("is_hd") or d.get("isHd") or d.get("hd")) or None,
        "is_new": bool(d.get("is_new") or d.get("isNew")) or None,
        "id": str(d["id"]) if d.get("id") is not None else None, "online": True,
    }


_STRONG_NAME = ("username", "user_name", "userName", "nickname", "nick", "displayName", "display_name", "model", "modelName")


def _is_room(x):
    """A real room has a username-like key, or a generic name PLUS viewers / a thumbnail: category and filter lists have neither."""
    if not isinstance(x, dict):
        return False
    if any(isinstance(x.get(k), str) and x[k].strip() for k in _STRONG_NAME):
        return True
    return bool(_first(x, ("name", "slug"))) and (_first(x, _K_VIEWERS) is not None or _first(x, _K_THUMB) is not None)


def _room_list(data):
    """The biggest list of room-like objects anywhere in a JSON document."""
    best = []

    def walk(n, depth=0):
        nonlocal best
        if depth > 6:
            return
        if isinstance(n, dict):
            for v in n.values():
                walk(v, depth + 1)
        elif isinstance(n, list):
            dicts = [x for x in n if isinstance(x, dict)]
            rooms = [x for x in dicts if _is_room(x)]
            if len(rooms) >= 2 and len(rooms) >= len(dicts) * 0.6 and len(rooms) > len(best):
                best = rooms
            for v in n[:5]:
                walk(v, depth + 1)

    walk(data)
    return best


def models_from_json(data, base=HOME_URL):
    """Find every list of cam-like objects anywhere in a JSON document -> {items: [...]}."""
    items, seen = [], set()
    for d in _room_list(data):
        c = _cam_from_dict(d, base)
        key = c and (c.get("link") or c["username"])
        if c and key not in seen:
            seen.add(key)
            items.append(c)
    return {"items": items}


# ----------------------------------------------------------------------------- fetching
_CACHE = {}                 # url -> (timestamp, result)
_CACHE_TTL = 90             # seconds: thumbnails are live, but the site should not be hammered
_RENDER_LOCK = threading.Lock()
_DISCOVERED = {"ts": 0.0, "urls": []}       # API endpoints found by discovery (kept in memory)
_DISCOVER_TTL = 6 * 3600

_API_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.lemoncams.com", "Referer": "https://www.lemoncams.com/"}


def _count(result):
    return len((result or {}).get("items", []))


class _HttpError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def _get_json(client, url):
    r = client.get(url)
    if r.status_code >= 400:
        raise _HttpError(r.status_code, f"HTTP {r.status_code}")
    text = r.text
    if text.lstrip()[:1] == "<":           # a blocked IP gets an HTML challenge page with status 200
        low = text[:4000].lower()
        raise _HttpError(403, "bot challenge" if any(k in low for k in ("just a moment", "cf-chl", "captcha", "attention required"))
                         else "HTML instead of JSON")
    return r.json()


def _get_json_retry(client, url):
    """One retry (with jitter) for the failures that are usually momentary: 429, 5xx, timeouts, dropped connections."""
    try:
        return _get_json(client, url)
    except Exception as e:
        st = getattr(e, "status", 0)
        transient = st == 429 or st >= 500 or isinstance(e, (httpx.TimeoutException, httpx.TransportError))
        if not transient:
            raise
        time.sleep(0.3 + random.random() * 0.6)
        return _get_json(client, url)


def _try_api(api_urls, base, diag):
    items, seen = [], set()
    with httpx.Client(timeout=20.0, follow_redirects=True, headers=_API_HEADERS) as c:
        for u in api_urls:
            try:
                res = models_from_json(_get_json(c, u), base)
                diag.append(f"api {u}: {len(res['items'])} cams")
                for it in res["items"]:
                    k = it.get("link") or it["username"]
                    if k not in seen:
                        seen.add(k)
                        items.append(it)
            except Exception as e:
                diag.append(f"api {u}: {type(e).__name__}: {str(e)[:100]}")
    return {"items": items} if items else None


def _try_html(url, diag):
    """The page's own HTML. Returns (result|None, html|None)."""
    from scraper import fetch_html          # reuses the app's HTTP client (headers, retries, timeouts)
    try:
        html, final = fetch_html(url, timeout=25.0, referer=HOME_URL)
    except Exception as e:
        diag.append(f"html: {type(e).__name__}: {str(e)[:100]}")
        return None, None
    res = parse_home(html, final)
    if res["items"]:
        diag.append(f"html: {len(res['items'])} cams")
        return res, html
    shell = "<app-root" in html and "posts__item" not in html
    diag.append("html: no cams in the page" + (" (a JavaScript-rendered shell)" if shell else ""))
    return None, html


# ---- API discovery: the shell names its scripts; the scripts name the API paths --------------------------------
_API_HOST_RX = re.compile(r"https://api[\w.\-]*lemoncams\.com")
_PATH_QUOTED = re.compile(r"""["'`](/[A-Za-z][\w\-]*(?:/[\w\-]+){0,4})/?(?:\?[^"'`]*)?["'`]""")
_PATH_TAIL = re.compile(r"""\}(/[A-Za-z][\w\-]*(?:/[\w\-]+){0,4})""")
_RANK_WORDS = ("cam", "model", "online", "live", "top", "home", "popular", "perform", "stream", "list",
               "search", "private", "feed", "main", "country", "browse")
_SKIP_PREFIX = ("/assets", "/static", "/blog", "/cdn", "/images", "/img", "/fonts", "/login", "/signup", "/register",
                "/privacy", "/terms", "/dmca", "/contact", "/about", "/favicon", "/legal", "/cookie")


def _script_urls(shell_html, base):
    soup = BeautifulSoup(shell_html, "lxml")
    host = (urlparse(base).hostname or "").lower()
    urls = []
    for tag in soup.find_all(["script", "link"]):
        u = tag.get("src") or tag.get("href") or ""
        rel = tag.get("rel") or []
        if not re.search(r"\.m?js(?:\?|$)", u, re.I):
            continue
        if tag.name == "script" or "modulepreload" in rel or "preload" in rel:
            full = urljoin(base, u)
            if (urlparse(full).hostname or "").lower().endswith(host.replace("www.", "")):
                urls.append(full)
    return list(dict.fromkeys(urls))[:30]


def _candidate_paths(js_text, words=None):
    words = words or _RANK_WORDS
    paths = set(_PATH_QUOTED.findall(js_text)) | set(_PATH_TAIL.findall(js_text))
    out = []
    for p in paths:
        low = p.lower()
        if low.startswith(_SKIP_PREFIX) or "." in p or not (3 <= len(p) <= 60):
            continue
        out.append((sum(w in low for w in words), p))
    out.sort(key=lambda t: (-t[0], len(t[1]), t[1]))
    ranked = [p for score, p in out if score > 0][:40]
    return ranked if len(ranked) >= 12 else ranked + [p for score, p in out if score == 0][:16 - len(ranked)]


def _discover_api(shell_html, base, diag):
    """Find a JSON endpoint of the site's own API that returns cams. Bounded: <=30 script files, <=80 requests."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if _DISCOVERED["urls"] and time.time() - _DISCOVERED["ts"] < _DISCOVER_TTL:
        return _DISCOVERED["urls"]
    js_urls = _script_urls(shell_html, base)
    if not js_urls:
        diag.append("discover: the page lists no script files")
        return []
    texts = []
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": UA}) as c:
        for u in js_urls:
            try:
                r = c.get(u)
                if r.status_code == 200:
                    texts.append(r.text[:6_000_000])
            except Exception:
                pass
    blob = "\n".join(texts)
    hosts = _API_HOST_RX.findall(shell_html) + _API_HOST_RX.findall(blob)
    api = max(set(hosts), key=hosts.count) if hosts else "https://api-v2-prod.lemoncams.com"
    cands = _candidate_paths(blob)
    diag.append(f"discover: {len(js_urls)} scripts read, API host {api}, {len(cands)} candidate paths")

    def probe(path):
        with httpx.Client(timeout=12.0, follow_redirects=True, headers=_API_HEADERS) as c:
            for suffix in ("", "?limit=48"):
                try:
                    n = len(models_from_json(_get_json(c, api + path + suffix), base)["items"])
                    if n >= 4:
                        return api + path + suffix, n
                except Exception:
                    continue
        return None

    hits = []
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(probe, p) for p in cands]
            for f in as_completed(futs, timeout=40):
                r = f.result()
                if r:
                    hits.append(r)
    except Exception as e:
        diag.append(f"discover: stopped early ({type(e).__name__})")
    hits.sort(key=lambda t: -t[1])
    urls = [u for u, _ in hits[:3]]
    if urls:
        _DISCOVERED.update(ts=time.time(), urls=urls)
        diag.append("discover: found " + ", ".join(f"{u} ({n})" for u, n in hits[:3]))
    else:
        diag.append("discover: none of the candidate paths returned cams (" + ", ".join(cands[:8]) + ")")
    return urls


_LOAD_MORE_JS = r"""() => {
  const b = [...document.querySelectorAll('button, a, .btn')].find(e => /^\s*(load|show|see|view)\s+more\b/i.test(e.textContent || '') && e.offsetParent);
  if (b) { b.click(); return true; } return false; }"""


def _render_page(url, selector, diag, scroll=False, budget=60.0):
    """Open `url` in a headless browser, wait for `selector`, optionally scroll / press 'load more' until the number of
    matches stops growing (infinite scroll = ALL the cams of the page).  -> html | None."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        diag.append("browser: Playwright is not installed on this server")
        return None
    try:
        with _RENDER_LOCK, sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(user_agent=UA)
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_selector(selector, timeout=15000)
                if scroll:
                    t0, last, still = time.time(), -1, 0
                    while time.time() - t0 < budget and still < 3:
                        n = len(page.query_selector_all(selector))
                        still = still + 1 if n == last else 0
                        last = n
                        page.mouse.wheel(0, 30000)
                        try:
                            page.evaluate(_LOAD_MORE_JS)
                        except Exception:
                            pass
                        page.wait_for_timeout(900)
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        diag.append(f"browser: {type(e).__name__}: {str(e)[:120]}")
        return None


def _try_render(url, diag, scroll=False):
    html = _render_page(url, ".posts__item--card", diag, scroll=scroll)
    if html is None:
        return None
    res = parse_home(html, url)
    diag.append(f"browser: {len(res['items'])} cams" + (" (scrolled to the end)" if scroll else ""))
    return res if res["items"] else None


def _allowed(url):
    p = urlparse(url)
    return p.scheme in ("http", "https") and (p.hostname or "").lower().endswith("lemoncams.com")


def _fetch_lemoncams(url=None, force=False):
    url = url or HOME_URL
    if not _allowed(url):
        return {"error": "Only lemoncams.com pages can be loaded here.", "items": [], "diagnostics": []}
    hit = _CACHE.get(url)
    if hit and not force and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    diag, result, source, shell = [], None, None, None
    configured = [u.strip() for u in os.environ.get("LEMONCAMS_API_URL", "").split(",") if u.strip()]
    is_home = url.rstrip("/") == HOME_URL.rstrip("/")
    known = (configured or (_DISCOVERED["urls"] if time.time() - _DISCOVERED["ts"] < _DISCOVER_TTL else [])) if is_home else []
    if known:
        result, source = _try_api(known, url, diag), "api"
    if not result:
        result, shell = _try_html(url, diag)
        source = "html"
    if not result and shell is not None and is_home:
        found = _discover_api(shell, url, diag)
        if found:
            result, source = _try_api(found, url, diag), "api"
    if not result:
        result, source = _try_render(url, diag, scroll=not is_home), "browser"

    if not result:
        return {"page": url, "items": [], "count": 0, "diagnostics": diag,
                "error": ("Lemoncams draws its cams with JavaScript, so the server only receives an empty page "
                          "and no data endpoint could be found automatically. Set LEMONCAMS_API_URL to the JSON "
                          "request that returns the cams (browser DevTools \u25B8 Network \u25B8 Fetch/XHR on "
                          "lemoncams.com), or install Playwright on the server.")}
    out = {"page": url, "items": result["items"], "count": len(result["items"]), "source": source,
           "diagnostics": diag, "fetched_at": int(time.time())}
    _CACHE[url] = (time.time(), out)
    return out


# ----------------------------------------------------------------------------- direct platform feeds
def _pages(tmpl, values):
    return [tmpl.format(n=v) for v in values]


_CB_WM = os.environ.get("CHATURBATE_WM", "dvafl")
_DIRECT = {
    # each platform: base + GROUPS.  Every group is fetched and merged (e.g. girls / men / trans / couples); inside a
    # group the first CANDIDATE (a list of paged URLs) that yields cams wins, the others are fallbacks for when an
    # endpoint changes or is blocked.  Paging goes deep: that is how "all the cams that can be scraped" are reached.
    "chaturbate": {"base": "https://chaturbate.com", "groups": [[
        _pages("https://chaturbate.com/api/ts/roomlist/room-list/?enable_recommendations=false&limit=90&offset={n}",
               range(0, 90 * 12, 90)),
        _pages("https://chaturbate.com/api/public/affiliates/onlinerooms/?wm=" + _CB_WM + "&format=json&limit=100&offset={n}",
               range(0, 100 * 10, 100)),
    ]]},
    "stripchat": {"base": "https://stripchat.com", "groups": [
        [_pages("https://stripchat.com/api/front/v2/models?limit=60&offset={n}&primaryTag=" + tag + "&sortBy=stripRanking",
                range(0, 60 * 10, 60)),
         _pages("https://stripchat.com/api/front/models?limit=60&offset={n}&primaryTag=" + tag + "&sortBy=stripRanking",
                range(0, 60 * 6, 60))]
        for tag in ("girls", "men", "trans", "couples")]},
    "cam4": {"base": "https://www.cam4.com", "groups": [
        [_pages("https://www.cam4.com/directoryCams?directoryJson=true&online=true&url=true&page={n}"
                "&resultsPerPage=60&gender=" + g, range(1, 8))]
        for g in ("female", "male", "shemale", "couple")]},
    "camsoda": {"base": "https://www.camsoda.com", "groups": [[
        _pages("https://www.camsoda.com/api/v1/browse/react?p={n}&perPage=60", range(1, 9)),
    ]]},
}


def _enabled_providers():
    want = [x.strip().lower() for x in os.environ.get("CAM_PROVIDERS", "").split(",") if x.strip()]
    return [p for p in (want or list(_DIRECT)) if p in _DIRECT]


def _fetch_direct(name):
    """-> (cams, notes) for one platform. Never raises. All pages of a candidate are requested in parallel; the groups
    of a platform are fetched one after the other and merged."""
    spec = _DIRECT[name]
    base = spec["base"]
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
               "Referer": base + "/", "Origin": base, "X-Requested-With": "XMLHttpRequest"}
    notes, cams, seen = [], [], set()

    def one(u):
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as c:
                return u, _get_json_retry(c, u), None
        except Exception as e:
            return u, None, e

    for group in spec["groups"]:
        got = 0
        for cand in group:
            with ThreadPoolExecutor(max_workers=min(12, len(cand))) as pool:
                fetched = list(pool.map(one, cand))              # keeps page order
            for idx, (u, data, exc) in enumerate(fetched):
                where = u.split("?")[0]
                if exc is not None:
                    if idx == 0:
                        notes.append(f"{name}: {where} -> {type(exc).__name__}: {str(exc)[:80]}")
                    continue
                new = 0
                for d in _room_list(data):
                    cam = _cam_from_dict(d, base, name, need_thumb=False)
                    if cam and cam["username"].lower() not in seen:
                        seen.add(cam["username"].lower())
                        cams.append(cam)
                        new += 1
                        got += 1
                if not new and idx == 0:
                    notes.append(f"{name}: {where} returned no usable rooms")
            if got:
                break                                              # this group is served: skip its fallbacks
    if cams:
        notes.append(f"{name}: {len(cams)} cams")
    return cams, notes


# ----------------------------------------------------------------------------- lemoncams, in the background
_LEMON = {"fut": None, "items": [], "ts": 0.0, "next_try": 0.0, "note": ""}
_LEMON_TTL, _LEMON_RETRY = 120, 900
_LEMON_POOL = ThreadPoolExecutor(max_workers=1)


def _lemon_job():
    try:
        r = _fetch_lemoncams(None, force=True)
        items = r.get("items") or []
        _LEMON.update(items=items, ts=time.time(), note=f"lemoncams: {len(items)} cams" if items
                      else "lemoncams: " + "; ".join((r.get("diagnostics") or [])[-2:])[:200])
        if not items:
            _LEMON["next_try"] = time.time() + _LEMON_RETRY      # JS-only site: do not hammer it
    except Exception as e:
        _LEMON.update(note=f"lemoncams: {type(e).__name__}: {str(e)[:100]}", next_try=time.time() + _LEMON_RETRY)


def _lemon_items(wait=3.0):
    """Cams lemoncams gave us so far (waits briefly for a running attempt, never blocks long)."""
    now = time.time()
    fut = _LEMON["fut"]
    if (fut is None or fut.done()) and now >= _LEMON["next_try"] and now - _LEMON["ts"] > _LEMON_TTL:
        fut = _LEMON["fut"] = _LEMON_POOL.submit(_lemon_job)
    if fut is not None and not fut.done():
        try:
            fut.result(timeout=wait)
        except Exception:
            pass
    return list(_LEMON["items"]), _LEMON["note"]


# ----------------------------------------------------------------------------- merge + public entry point
def _finish(cam):
    """Same shape for every card, wherever it came from; lemoncams cards get the platform's own room link."""
    prov = (cam.get("provider") or "").lower()
    if prov in _ROOM_URL and cam.get("username"):
        direct = _ROOM_URL[prov].format(u=quote(cam["username"]))
        if cam.get("link") != direct:
            cam["lemoncams_link"] = cam.get("link")
            cam["link"] = direct
    if not cam.get("provider_name"):
        cam["provider_name"] = _PROVIDER_LABEL.get(prov, prov.title() or None)
    thumbs = []
    for u in [cam.get("thumbnail")] + list(cam.get("thumbnails") or []) + _default_thumbs(prov, cam.get("username")):
        _add_thumb(thumbs, u)
    cam["thumbnails"] = thumbs[:5]
    cam["thumbnail"] = thumbs[0] if thumbs else None
    cam.setdefault("_seen", int(time.time()))
    cam["tags"] = []                                   # tags are gone: only Lemoncams categories + countries filter the cams
    for k in ("categories", "languages"):
        cam[k] = cam.get(k) or []
    for k in ("country_code", "flag_emoji", "gender", "location", "hd", "is_new"):
        cam.setdefault(k, None)
    if not cam.get("flag_emoji") and cam.get("country"):
        cam["country_code"] = cam.get("country_code") or _country(cam["country"])[1]
        cam["flag_emoji"] = _flag(cam["country_code"])
    return cam


_MAX_CAMS = 6000


def _merge(groups):
    """groups: {source: [cams]} -> one list: each platform sorted by viewers, then interleaved round-robin.
    A cam that appears twice (a platform feed + a Lemoncams page) is ONE card that keeps every category."""
    by_key, per = {}, {}
    for src, cams in groups.items():
        for cam in cams:
            cam = _finish(cam)
            prov = cam.get("provider") or src
            key = (prov, (cam.get("username") or "").lower())
            if not cam.get("link"):
                continue
            old = by_key.get(key)
            if old is not None:
                for c in cam.get("categories") or []:
                    if c.lower() not in {x.lower() for x in old["categories"]}:
                        old["categories"].append(c)
                for k in ("country", "country_code", "flag_emoji", "gender", "age", "room_title"):
                    if not old.get(k) and cam.get(k):
                        old[k] = cam[k]
                if cam.get("lemoncams_link") and not old.get("lemoncams_link"):
                    old["lemoncams_link"] = cam["lemoncams_link"]
                continue
            by_key[key] = cam
            per.setdefault(prov, []).append(cam)
    for lst in per.values():
        lst.sort(key=lambda c: -(c.get("viewers") or 0))
    out, i = [], 0
    while any(i < len(v) for v in per.values()) and len(out) < _MAX_CAMS:
        for v in per.values():
            if i < len(v):
                out.append(v[i])
        i += 1
    return out


# ----------------------------------------------------------------------------- the ONLY filters: categories + countries
CATEGORIES_URL = "https://www.lemoncams.com/categories"
COUNTRIES_URL = "https://www.lemoncams.com/world-map-of-sex-cams"
_FILTERS = {"ts": 0.0, "out": None}
_FILTERS_TTL = 6 * 3600
_NAV_FIRST = {"blog", "login", "signup", "register", "privacy", "terms", "dmca", "contact", "about", "legal", "cookie",
              "cookies", "faq", "help", "support", "assets", "static", "favicon", "sitemap", "2257", "tos", "advertise",
              "webmasters", "cam-reviews", "reviews"}
_NAV_PATHS = {"/categories", "/world-map-of-sex-cams", "/home", "/new", "/popular", "/live"}
_COUNTRY_NAMES = {v.lower() for k, v in _COUNTRIES.items()} | set(_NAME2CODE)
_FILTER_WORDS = {"categories": ("categor", "genre", "niche", "section", "type"),
                 "countries": ("countr", "nation", "world", "map", "location", "region", "flag")}
_DISCOVERED_F = {}                 # kind -> (timestamp, [json urls])


def _is_country_name(name):
    return (name or "").strip().lower() in _COUNTRY_NAMES


def _name_count(a):
    """Anchor -> (clean name, count|None).  A separate numeric child is the count; otherwise a trailing number is."""
    count = None
    for ch in a.find_all(["span", "small", "em", "b", "i", "div"]):
        t = _txt(ch)
        if re.fullmatch(r"\(?\d[\d.,]*\)?", t or ""):
            count = _int(t)
            ch.extract()
    text = _txt(a) or (a.get("title") or a.get("aria-label") or "").strip()
    if count is None:
        m = re.match(r"^(.*[A-Za-z\u00C0-\u024F].*?)\s+\(?(\d[\d.,]*)\)?$", text)
        if m:
            text, count = m.group(1), _int(m.group(2))
    return re.sub(r"\s+", " ", text).strip(), count


def parse_filter_links(html, base, kind):
    """Rendered Lemoncams /categories or /world-map-of-sex-cams page -> [{name, slug, url, count, code?}].
    Only links inside the page body count: header, footer and menus are dropped."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    for t in soup.select("app-navigation, app-footer, footer, header, nav, #header-mobile, .posts__item--card"):
        t.decompose()
    root = soup.find("app-root") or soup.body or soup
    rows, seen = [], set()
    for a in root.find_all("a"):
        full = _abs(base, a.get("href") or a.get("xlink:href"))
        if not full or not _allowed(full) or full in seen:
            continue
        path = urlparse(full).path.rstrip("/")
        segs = [x for x in path.split("/") if x]
        if not segs or len(segs) > 3 or path.lower() in _NAV_PATHS or segs[0].lower() in _NAV_FIRST:
            continue
        flag_img = a.select_one("img[src*='flag'], img.flag-image")
        code = None
        if flag_img is not None:
            fm = re.search(r"/([A-Za-z]{2})\.(?:svg|png|webp|jpg|gif)", flag_img.get("src") or flag_img.get("data-src") or "")
            code = fm.group(1).upper() if fm else None
        name, count = _name_count(a)
        if not (2 <= len(name) <= 40):
            continue
        looks_country = bool(code) or _is_country_name(name) or any("countr" in x.lower() for x in segs[:-1])
        if (kind == "countries") != looks_country:
            continue
        if kind == "countries" and not code:
            code = _NAME2CODE.get(name.lower())
        seen.add(full)
        rows.append({"name": name, "slug": segs[-1], "url": full, "count": count, "code": code})
    return rows


def _filter_row(x, kind):
    """One JSON object of a category / country list -> row | None."""
    if _first(x, _STRONG_NAME[:7]) or _first(x, ("viewers", "viewersCount", "num_users", "viewerCount")) is not None:
        return None                                     # that is a cam, not a filter entry
    name = _first(x, ("name", "title", "label", "displayName", "display_name", "countryName", "country", "text"))
    if isinstance(name, dict):
        name = _first(name, ("en", "name", "default"))
    if not isinstance(name, str) or not (2 <= len(name.strip()) <= 40):
        return None
    name = name.strip()
    raw_code = _first(x, ("code", "countryCode", "country_code", "iso", "iso2", "alpha2", "cc"))
    code = raw_code.upper() if isinstance(raw_code, str) and re.fullmatch(r"[A-Za-z]{2}", raw_code) else None
    is_country = bool(code) or _is_country_name(name)
    if (kind == "countries") != is_country:
        return None
    if kind == "countries":
        code = code or _NAME2CODE.get(name.lower())
        if code and name.upper() == code:
            name = _COUNTRIES.get(code, name)
    slug = _first(x, ("slug", "seo", "seoName", "alias", "code", "id"))
    link = _as_url(_first(x, ("url", "link", "href", "permalink")), HOME_URL)
    cnt = _first(x, ("count", "total", "modelsCount", "models_count", "cams", "camsCount", "online", "onlineCount", "num"))
    return {"name": name, "slug": str(slug) if slug is not None else None, "url": link if link and _allowed(link) else None,
            "count": _to_int(cnt), "code": code}


def _filters_from_json(data, kind):
    """The longest list of filter-like objects anywhere in a JSON document."""
    best = []

    def walk(n, depth=0):
        nonlocal best
        if depth > 7:
            return
        if isinstance(n, dict):
            for v in n.values():
                walk(v, depth + 1)
            if n and all(isinstance(v, (str, int)) for v in n.values()) and kind == "countries" and len(n) >= 5:
                rows = [{"name": str(v), "slug": str(k), "url": None, "count": None,
                         "code": str(k).upper() if re.fullmatch(r"[A-Za-z]{2}", str(k)) else None}
                        for k, v in n.items() if isinstance(v, str) and _is_country_name(v)]
                if len(rows) >= 5 and len(rows) > len(best):
                    best = rows                       # {"US": "United States", ...}
        elif isinstance(n, list):
            dicts = [x for x in n if isinstance(x, dict)]
            if len(dicts) >= 3 and len(dicts) >= len(n) * 0.7:
                rows = [r for r in (_filter_row(x, kind) for x in dicts) if r]
                if len(rows) >= 3 and len(rows) >= len(dicts) * 0.6 and len(rows) > len(best):
                    best = rows
            for v in n[:6]:
                walk(v, depth + 1)

    walk(data)
    out, seen = [], set()
    for r in best:
        k = (r["code"] or r["name"]).lower()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _filters_try_api(urls, kind, diag):
    with httpx.Client(timeout=20.0, follow_redirects=True, headers=_API_HEADERS) as c:
        for u in urls:
            try:
                rows = _filters_from_json(_get_json(c, u), kind)
                diag.append(f"{kind} api {u}: {len(rows)} entries")
                if rows:
                    return rows
            except Exception as e:
                diag.append(f"{kind} api {u}: {type(e).__name__}: {str(e)[:100]}")
    return None


def _discover_filter_api(shell_html, base, kind, diag):
    """Same idea as _discover_api, but the probe looks for a LIST OF CATEGORIES / COUNTRIES."""
    hit = _DISCOVERED_F.get(kind)
    if hit and time.time() - hit[0] < _DISCOVER_TTL:
        return hit[1]
    js_urls = _script_urls(shell_html, base)
    if not js_urls:
        diag.append(f"discover {kind}: the page lists no script files")
        return []
    texts = []
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": UA}) as c:
        for u in js_urls:
            try:
                r = c.get(u)
                if r.status_code == 200:
                    texts.append(r.text[:6_000_000])
            except Exception:
                pass
    blob = "\n".join(texts)
    hosts = _API_HOST_RX.findall(shell_html) + _API_HOST_RX.findall(blob)
    api = max(set(hosts), key=hosts.count) if hosts else "https://api-v2-prod.lemoncams.com"
    cands = _candidate_paths(blob, _FILTER_WORDS[kind])
    diag.append(f"discover {kind}: {len(js_urls)} scripts, API host {api}, {len(cands)} candidate paths")

    def probe(path):
        with httpx.Client(timeout=12.0, follow_redirects=True, headers=_API_HEADERS) as c:
            try:
                n = len(_filters_from_json(_get_json(c, api + path), kind))
                return (api + path, n) if n >= 3 else None
            except Exception:
                return None

    hits = []
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for f in as_completed([ex.submit(probe, p) for p in cands], timeout=40):
                if f.result():
                    hits.append(f.result())
    except Exception as e:
        diag.append(f"discover {kind}: stopped early ({type(e).__name__})")
    hits.sort(key=lambda t: -t[1])
    urls = [u for u, _ in hits[:3]]
    if urls:
        _DISCOVERED_F[kind] = (time.time(), urls)
        diag.append(f"discover {kind}: found " + ", ".join(f"{u} ({n})" for u, n in hits[:3]))
    else:
        diag.append(f"discover {kind}: no candidate path returned a list")
    return urls


def _fetch_filter_kind(kind, url, env_name, diag):
    """-> rows for ONE filter row (categories | countries): configured API -> page HTML -> discovered API -> browser."""
    configured = [u.strip() for u in os.environ.get(env_name, "").split(",") if u.strip()]
    if configured:
        rows = _filters_try_api(configured, kind, diag)
        if rows:
            return rows
    shell = None
    try:
        from scraper import fetch_html
        html, final = fetch_html(url, timeout=25.0, referer=HOME_URL)
        rows = parse_filter_links(html, final, kind)
        if rows:
            diag.append(f"{kind}: {len(rows)} entries from the page HTML")
            return rows
        shell = html
        diag.append(f"{kind}: no entries in the page HTML (JavaScript shell)")
    except Exception as e:
        diag.append(f"{kind} html: {type(e).__name__}: {str(e)[:100]}")
    if shell is not None:
        found = _discover_filter_api(shell, url, kind, diag)
        if found:
            rows = _filters_try_api(found, kind, diag)
            if rows:
                return rows
    html = _render_page(url, "a[href]", diag, scroll=False)
    if html:
        rows = parse_filter_links(html, url, kind)
        diag.append(f"{kind}: {len(rows)} entries from the rendered page")
        if rows:
            return rows
    return []


def _derive_filters(items):
    """Last resort when Lemoncams' own lists cannot be read: the real categories / countries of the cams we DO have."""
    cats, ctry = {}, {}
    for c in items:
        for n in c.get("categories") or []:
            cats[n.lower()] = (n, cats.get(n.lower(), (n, 0))[1] + 1)
        if c.get("country"):
            k = c["country"].lower()
            ctry[k] = (c["country"], c.get("country_code"), ctry.get(k, (0, 0, 0))[2] + 1)
    return (
        [{"name": n, "slug": None, "url": None, "count": k, "code": None}
         for n, k in sorted(cats.values(), key=lambda t: -t[1])],
        [{"name": n, "slug": None, "url": None, "count": k, "code": code}
         for n, code, k in sorted(ctry.values(), key=lambda t: -t[2])],
    )


def fetch_lemon_filters(force=False):
    """{categories: [...], countries: [...]} from lemoncams.com/categories and lemoncams.com/world-map-of-sex-cams."""
    if _FILTERS["out"] and not force and time.time() - _FILTERS["ts"] < _FILTERS_TTL:
        return _FILTERS["out"]
    diag, res = [], {}
    jobs = (("categories", CATEGORIES_URL, "LEMONCAMS_CATEGORIES_API"),
            ("countries", COUNTRIES_URL, "LEMONCAMS_COUNTRIES_API"))
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {}
        for k, u, e in jobs:
            d = []
            futs[k] = (ex.submit(_fetch_filter_kind, k, u, e, d), d)
        for k, (f, d) in futs.items():
            try:
                res[k] = f.result(timeout=150)
            except Exception as e:
                res[k] = []
                d.append(f"{k}: {type(e).__name__}: {str(e)[:100]}")
            diag.extend(d)
    source = {k: "lemoncams" for k in res if res[k]}
    if not res.get("categories") or not res.get("countries"):
        cached = (_ALL_CACHE.get("out") or {}).get("items") or []
        dc, dn = _derive_filters(cached)
        for k, rows in (("categories", dc), ("countries", dn)):
            if not res.get(k) and rows:
                res[k], source[k] = rows, "derived from the loaded cams"
                diag.append(f"{k}: Lemoncams' list could not be read, showing the {len(rows)} found on the loaded cams")
    out = {"categories": res.get("categories") or [], "countries": res.get("countries") or [], "source": source,
           "diagnostics": diag, "fetched_at": int(time.time())}
    if out["categories"] or out["countries"]:
        if all(v == "lemoncams" for v in source.values()) and len(source) == 2:
            _FILTERS.update(ts=time.time(), out=out)          # cache only the real thing; derived lists are retried
    return out


# ----------------------------------------------------------------------------- crawl: every category + country page
# Each Lemoncams category / country page is read once in the background and merged into the big list, so cams are
# found with their real categories and countries.  The request never waits for it: it returns what is known so far.
_CRAWL = {"ts": 0.0, "running": False, "cams": {}, "done": 0, "total": 0, "note": ""}
_CRAWL_LOCK = threading.Lock()
_CRAWL_TTL, _CRAWL_BUDGET, _CRAWL_MAX_PAGES = 15 * 60, 420.0, 400


def _annotate(cams, kind, name, code=None):
    for c in cams:
        c = dict(c)
        if kind == "categories":
            c["categories"] = list(dict.fromkeys((c.get("categories") or []) + [name]))
        elif kind == "countries" and not c.get("country"):
            c["country"], c["country_code"] = name, code or _country(name)[1]
        yield c


def _crawl_add(cams):
    with _CRAWL_LOCK:
        for c in cams:
            key = ((c.get("provider") or "").lower(), (c.get("username") or "").lower())
            old = _CRAWL["cams"].get(key)
            if old is None:
                _CRAWL["cams"][key] = c
            else:
                old["categories"] = list(dict.fromkeys((old.get("categories") or []) + (c.get("categories") or [])))
                for k in ("country", "country_code"):
                    if not old.get(k) and c.get(k):
                        old[k] = c[k]


def _crawl_job(force=False):
    t0 = time.time()
    try:
        f = fetch_lemon_filters(force=force)
        targets = [(k, r) for k in ("categories", "countries") for r in f.get(k, []) if r.get("url")][:_CRAWL_MAX_PAGES]
        with _CRAWL_LOCK:
            _CRAWL.update(done=0, total=len(targets), note="" if targets else "no category / country page links to read")

        def one(t):
            kind, row = t
            if time.time() - t0 > _CRAWL_BUDGET:
                return
            try:
                r = _fetch_lemoncams(row["url"], force=force)
                _crawl_add(_annotate(r.get("items") or [], kind, row["name"], row.get("code")))
            except Exception:
                pass
            finally:
                with _CRAWL_LOCK:
                    _CRAWL["done"] += 1

        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(one, targets))
    except Exception as e:
        _CRAWL["note"] = f"{type(e).__name__}: {str(e)[:100]}"
    finally:
        _CRAWL.update(running=False, ts=time.time())


def _ensure_crawl(force=False):
    with _CRAWL_LOCK:
        if _CRAWL["running"] or (not force and time.time() - _CRAWL["ts"] < _CRAWL_TTL):
            return
        _CRAWL["running"] = True
    threading.Thread(target=_crawl_job, args=(force,), daemon=True).start()


def _crawl_status():
    return {"running": _CRAWL["running"], "done": _CRAWL["done"], "total": _CRAWL["total"],
            "cams": len(_CRAWL["cams"]), "note": _CRAWL["note"]}


# ----------------------------------------------------------------------------- public entry point
_ALL_CACHE = {"ts": 0.0, "out": None}


def fetch_livecams(url=None, force=False, kind=None, name=None):
    """No url: ALL the cams (platform feeds, deep paging + Lemoncams home + every category / country page found so far).
    With a Lemoncams url (a category / country chip): that one page, its cams labelled with `kind` / `name`."""
    if url:
        r = _fetch_lemoncams(url, force)
        items = list(r.get("items", []))
        if kind in ("categories", "countries") and name:
            items = list(_annotate(items, kind, name))
        r["items"] = [_finish(c) for c in items]
        r["count"] = len(r["items"])
        _register_thumbs(r["items"])
        return r
    if _ALL_CACHE["out"] and not force and time.time() - _ALL_CACHE["ts"] < _CACHE_TTL:
        _ensure_crawl()
        out = dict(_ALL_CACHE["out"])
        out["crawl"] = _crawl_status()
        return out

    providers = _enabled_providers()
    diag, groups = [], {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(providers)))
    futs = {ex.submit(_fetch_direct, p): p for p in providers}
    try:
        for f in as_completed(futs, timeout=60):
            plat = futs[f]
            try:
                cams, notes = f.result()
            except Exception as e:
                cams, notes = [], [f"{plat}: {type(e).__name__}: {str(e)[:100]}"]
            groups[plat] = cams
            diag.extend(notes)
    except Exception:
        diag.append(f"timeout: {', '.join(p for p in providers if p not in groups)} did not answer in time")
    ex.shutdown(wait=False, cancel_futures=True)

    lem, lem_note = _lemon_items(wait=0.5 if any(groups.values()) else 8.0)
    with _CRAWL_LOCK:
        crawled = list(_CRAWL["cams"].values())
    if lem or crawled:
        groups["lemoncams"] = list(lem) + crawled
    if lem_note:
        diag.append(lem_note)
    _ensure_crawl(force)

    items = _merge(groups)
    if not items:
        return {"page": HOME_URL, "items": [], "count": 0, "diagnostics": diag, "crawl": _crawl_status(),
                "error": ("No live cams could be loaded: none of the cam platforms answered this server. "
                          "Their servers may be blocking this host's IP address; see the diagnostics below. "
                          "Set LEMONCAMS_API_URL or CAM_PROVIDERS, or run the app from a residential/other host.")}
    by = {}
    for c in items:
        label = c.get("provider_name") or c.get("provider") or "?"
        by[label] = by.get(label, 0) + 1
    out = {"page": HOME_URL, "items": items, "count": len(items), "providers": by, "source": "direct",
           "platform_status": {p: {"ok": bool(groups.get(p)), "count": len(groups.get(p) or [])} for p in providers},
           "diagnostics": diag, "fetched_at": int(time.time())}
    _register_thumbs(items)
    _ALL_CACHE.update(ts=time.time(), out=out)
    res = dict(out)
    res["crawl"] = _crawl_status()
    return res


# ----------------------------------------------------------------------------- thumbnail proxy
# Stripchat's and Cam4's image CDNs refuse (or blank out) hot-linked requests that come from another site's
# page, so the browser shows dark tiles.  The server fetches those images itself, with the platform's own
# Referer, and hands them to the page from the app's own origin.  Only URLs that were scraped for a card
# can be requested (no open proxy).
_THUMB_SRC = {}                  # image URL -> provider it belongs to
_THUMB_CACHE = {}                # image URL -> (timestamp, bytes, content-type)
_THUMB_TTL, _THUMB_MAX, _THUMB_BYTES = 40, 300, 4_000_000


def _register_thumbs(items):
    if len(_THUMB_SRC) > 6000:
        _THUMB_SRC.clear()
    for c in items:
        for u in (c.get("thumbnails") or []) + [c.get("thumbnail")]:
            if u:
                _THUMB_SRC[u] = (c.get("provider") or "").lower()


_ORIGIN_OF = {"xlivetv": "https://xlivetv.com/"}          # providers that are not cam platforms


def register_thumb_urls(urls, provider):
    """Let /api/cam-thumb serve these image URLs (used by the Live Channels scraper)."""
    if len(_THUMB_SRC) > 6000:
        _THUMB_SRC.clear()
    for u in urls:
        if u:
            _THUMB_SRC[u] = provider


def fetch_thumb(url):
    """-> (bytes, content_type). Raises LookupError (not a scraped URL) or RuntimeError (upstream refused)."""
    url = re.sub(r"[?&]_t=\d+$", "", url or "")
    if url not in _THUMB_SRC:
        raise LookupError("unknown thumbnail")
    hit = _THUMB_CACHE.get(url)
    if hit and time.time() - hit[0] < _THUMB_TTL:
        return hit[1], hit[2]
    prov = _THUMB_SRC[url]
    site = _ORIGIN_OF.get(prov) or _ROOM_URL.get(prov, HOME_URL).split("{")[0]
    origin = "/".join(site.split("/")[:3]) + "/"
    headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9", "Referer": origin, "Origin": origin.rstrip("/")}
    with httpx.Client(timeout=12.0, follow_redirects=True, headers=headers) as c:
        r = c.get(url)
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    if not ctype.startswith("image/"):
        raise RuntimeError(f"not an image ({ctype or 'no content-type'})")
    if len(r.content) < 300 or len(r.content) > _THUMB_BYTES:
        raise RuntimeError(f"unusable image ({len(r.content)} bytes)")
    if len(_THUMB_CACHE) >= _THUMB_MAX:
        for k in sorted(_THUMB_CACHE, key=lambda k: _THUMB_CACHE[k][0])[: _THUMB_MAX // 3]:
            _THUMB_CACHE.pop(k, None)
    _THUMB_CACHE[url] = (time.time(), r.content, ctype)
    return r.content, ctype
