"""Extracted category and tag scraping."""
from collections import Counter
import hashlib
import re
import time
from urllib.parse import urljoin, urlparse

from .parsers import (
    absolute,
    is_image_url,
    looks_like_placeholder,
    is_acceptable_thumb,
    pick_from_srcset,
    clean_title,
    title_from_url,
    reg_domain,
)
from .shared import (
    CATEGORY_SKIP_PATH_PREFIXES,
    CATEGORY_SKIP_CLASS_HINTS,
    _NUMERIC_ONLY,
    _SITE_SECTIONS,
    _CHROME_TAGS,
    _CHROME_ATTR_RE,
    _LANG_HREF_RE,
    _MAIN_SELECTORS,
    _JUNK_TITLES,
    _DUR_RE,
    _NUM_RE,
    _PCT_RE,
)
from .utils import (
    _find_thumbnail,
    _record_default_images,
    _find_next_page_link,
    _current_page_number,
    _guess_next_page,
    fetch_html,
)


def _card_for_link(a):
    """Walk up parents to find a card that holds the thumbnail (may be sibling of the link)."""
    CARD_TOKENS = {
        "channel", "channels", "categories_card", "category-card", "category_card",
        "item", "card", "thumb", "studio", "model", "site-card", "sponsor",
        "thumb-serie", "categories_card_body",
    }
    candidates = []
    node = a.parent if a is not None else None
    for _ in range(6):
        if node is None or not getattr(node, "get", None):
            break
        tokens = {c.lower() for c in (node.get("class") or [])}
        joined = " ".join(tokens)
        hit = bool(tokens & CARD_TOKENS) or any(
            k in joined for k in (
                "categories_card", "category-card", "category_card",
                "site-card", "thumb-serie",
            )
        )
        if hit:
            candidates.append(node)
        node = node.parent
    for c in candidates:
        if c.find("img") or c.find("video"):
            return c
    return candidates[-1] if candidates else a


def _is_category_link(a, base_host):
    href = a.get("href", "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    full = urljoin("http://" + base_host, href) if not href.startswith("http") else href
    parsed = urlparse(full)
    if base_host and base_host not in parsed.netloc:
        return False
    path = (parsed.path or "/").lower()
    if any(path.startswith(skip) for skip in CATEGORY_SKIP_PATH_PREFIXES):
        return False
    card = _card_for_link(a)
    has_image = bool(a.find("img")) or bool(a.find("video")) or bool(card.find("img")) or bool(card.find("video"))
    path_ok = bool(re.search(
        r"/(?:categor(?:y|ies)|cat|tags?|c|video-category|studio|studios|"
        r"channel|channels|model|models|site|sites|network|networks|"
        r"series|sponsor|pornstar)/",
        path,
    ))
    if not has_image and not path_ok:
        return False
    cls_id = " ".join(list(a.get("class") or []) + [a.get("id") or ""]).lower()
    if any(k in cls_id for k in CATEGORY_SKIP_CLASS_HINTS):
        return False
    return True


def _clean_category_name(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"[\s\n]*[\d,]+\s*(?:videos?|vids?|clips?)?\s*$", "", text, flags=re.I).strip()
    if not text or len(text) < 2 or len(text) > 80:
        return None
    if _NUMERIC_ONLY.match(text):
        return None
    if _JUNK_TITLES.match(text):
        return None
    if re.search(r"\blogo\b", text, re.I):
        return None
    return text
