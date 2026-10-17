# ... add to shared
_COUNT_TAIL = re.compile(r"([\d][\d,]*)\s*(?:videos?|vids?|clips?)", re.I)
_SP_STAR_PATH = re.compile(r"^/pornstar/([^/?#]+)/?$", re.I)
_PVB_ACTOR_SECTIONS = re.compile(r"^(?:porn[_-]?actors?|actors?|pornstars?|zone_actor)$", re.I)
_PVB_NOT_A_STAR = re.compile(r"^(?:page|letter|alphabet|\d+)$", re.I)
_PVB_STAR_QS = ("actor", "star", "pornstar", "model")
_PVB_ALT_PREFIX = re.compile(r"^\s*porn\s*stars?\s+", re.I)
_MODEL_HREF_RE = re.compile(r"/(?:models?|pornstars?|actresses)/([^/?#]+)/?$", re.I)
