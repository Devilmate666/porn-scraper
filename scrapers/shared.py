"""Shared constants, patterns, and helper utilities for scrapers."""
import re
from urllib.parse import urljoin, urlparse

# Image and video extensions
IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|bmp|ico|avif|heic)(\?|#|/|$)", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|webm|ogg|ogv|mov|m4v|mkv|m3u8|mpd)(\?|#|/|$)", re.I)

# Placeholder and shared image detection
_PLACEHOLDER_HINTS = re.compile(
    r"(placeholder|lazy|loading|blank|transparent|spacer|1x1|no[-_]?image|preload|"
    r"default[-_]?thumb|/static/img/load|/img/load|load-\dx\d|data:image/gif|"
    r"data:image/svg|pixel\.(?:gif|png)|spacer\.(?:gif|png))",
    re.I,
)

_SHARED_IMAGE_RE = re.compile(
    r"(?:^|[/_\-])(logo|sprite|banner|share|sharing|"
    r"og[_-]?default|og[_-]?image|twitter[_-]?card|"
    r"default|noimage|no[_-]?image|dummy|"
    r"avatar|flag|icon|favicon|watermark|brand)(?:[/_\-.]|$)",
    re.I,
)

# Title filtering
_JUNK_TITLES = re.compile(
    r"^(link|links|watch|watch now|play|play now|view|view now|"
    r"download|download now|read more|more|click here|click|"
    r"open|open now|see more|show more|go|next|prev|previous|"
    r"video|photo|image|here|→|»|›|▶|►|•|\-|--)$",
    re.I,
)
JUNK_TITLES = _JUNK_TITLES

# Video hint attributes
VIDEO_HINT_ATTRS = (
    "data-poster", "data-video-poster", "data-video-thumb", "data-video-preview",
    "data-preview", "data-preview-src", "data-video", "data-video-src",
    "data-video-id", "data-video-url", "data-src-video", "poster",
)

# Path patterns
PHOTO_PATH_FRAGMENTS = (
    "/photo/", "/photos/", "/image/", "/images/", "/img/",
    "/album/", "/albums/", "/gallery/", "/galleries/",
    "/pic/", "/pics/", "/picture/", "/pictures/",
    "/image-gallery/", "/photo-gallery/",
)

VIDEO_PATH_FRAGMENTS = (
    "/video/", "/videos/", "/watch/", "/play/", "/player/",
    "/embed/", "/v/", "/clip/", "/clips/", "/movie/", "/movies/",
    "/stream/", "/tube/", "/p/",
    "/content_video/", "/content_video_alt/",  # pornvideobb-style sites
)

# Metadata patterns
_DUR_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
_NUM_RE = re.compile(r"\d[\d.,]*\s*[kKmMbB]?(?![a-zA-Z])")
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_META_SKIP_TEXT = re.compile(
    r"^(view later|watch later|share|download|play|more|hd|4k|full video|\d+)$", re.I)
_META_TYPES = (
    ("pornstar", r"/(?:pornstars?|models?|actors?|actresses|stars?|performers?|girls?)/", ("icon-star", "pornstar", "model")),
    ("series",   r"/series/",                                                             ("icon-series",)),
    ("studio",   r"/(?:studios?|sites?|networks?|channels?|sponsors?|producers?)/",       ()),
    ("uploader", r"/(?:users?|uploaders?|members?|profile|author|u)/",                     ("info-uploader", "uploader")),
    ("genre",    r"/(?:categor(?:y|ies)|cats?|tags?|genres?|niches?|c)/",                  ("icon-folder", "info-item", "categor", "genre", "tag")),
)

# Category constants
_CATEGORY_SKIP_PATH_PREFIXES = (
    "/login", "/signup", "/register", "/terms", "/privacy",
    "/contact", "/about", "/faq", "/dmca", "/page/",
)
CATEGORY_SKIP_PATH_PREFIXES = _CATEGORY_SKIP_PATH_PREFIXES
_CATEGORY_SKIP_CLASS_HINTS = (
    "navbar", "nav-", "-nav", "menu", "header", "footer",
    "breadcrumb", "pagination",
)
CATEGORY_SKIP_CLASS_HINTS = _CATEGORY_SKIP_CLASS_HINTS
_NUMERIC_ONLY = re.compile(r"^\d+$")
NUMERIC_ONLY = _NUMERIC_ONLY

# Studio/site sections
_STUDIO_PATH_RE = re.compile(r"/(?:sites?|studios?|channels?|networks?|sponsors?)/[^/?#]+", re.I)
STUDIO_PATH_RE = _STUDIO_PATH_RE
_SITE_SECTIONS = ("sites", "site", "networks", "network", "studios", "studio", "channels", "channel")
SITE_SECTIONS = _SITE_SECTIONS

# Chrome stripping
_CHROME_TAGS = ("header", "nav", "footer", "aside", "script", "style", "noscript")
CHROME_TAGS = _CHROME_TAGS
_CHROME_ATTR_RE = re.compile(
    r"(^|[\s_-])(lang|language|languages|locale|flags?|navbar|nav|menu|topbar|top-bar|"
    r"header|footer|breadcrumbs?|sidebar|dropdown|pagination)([\s_-]|$)", re.I)
CHROME_ATTR_RE = _CHROME_ATTR_RE
_LANG_HREF_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?)/?$", re.I)
LANG_HREF_RE = _LANG_HREF_RE
_MAIN_SELECTORS = (
    "main", "[role=main]", "#content", "#main", ".content", ".main-content",
    ".list-categories", ".categories", ".categories-list", ".category-list",
    ".container",
)
MAIN_SELECTORS = _MAIN_SELECTORS

# Shared caches (to avoid duplication)
_DEFAULT_IMAGE_CACHE = {}  # host -> set of default image paths
DEFAULT_IMAGE_CACHE = _DEFAULT_IMAGE_CACHE
_CACHE = {}  # unified cache for various components
