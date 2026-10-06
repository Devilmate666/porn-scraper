// ---------------------------------------------------------------------------
// Live scraping inside the Worker: a TypeScript port of the generic scraper in scraper.py
// (scrape_page / _extract_items_from_soup / search_one). Runs on every request, so search and scrolling
// hit the real websites instead of only the pre-scraped KV data.
//
// Kept CPU-light on purpose (Workers have a CPU budget): scripts/styles/comments are stripped with a regex
// before parsing, and card containers are found in ONE pass instead of re-walking the DOM per link.
// ---------------------------------------------------------------------------
import { parse, HTMLElement } from "node-html-parser";

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const MAX_HTML = 900_000;

export interface Item {
  title: string;
  thumbnail: string | null;
  video_src: string | null;
  link: string;
  page: string;
  uid: string;
  duration?: string;
  views?: string;
  rating?: string;
  added?: string;
  quality?: string;
  [k: string]: any;
}
export interface PageResult {
  page: string;
  page_title?: string;
  items: Item[];
  count: number;
  next_page: string | null;
  next_is_guess?: boolean;
  page_num: number | null;
  error?: string;
  [k: string]: any;
}

// ------------------------------------------------------------------ constants (from scraper.py)
export const IMAGE_EXT = /\.(jpe?g|png|gif|webp|svg|bmp|ico|avif|heic)(\?|#|\/|$)/i;
export const VIDEO_EXT = /\.(mp4|webm|ogg|ogv|mov|m4v|mkv|m3u8|mpd)(\?|#|\/|$)/i;
export const PLACEHOLDER = /(placeholder|lazy|loading|blank|transparent|spacer|1x1|no[-_]?image|preload|default[-_]?thumb|\/static\/img\/load|\/img\/load|load-\dx\d|data:image\/gif|data:image\/svg|pixel\.(?:gif|png)|spacer\.(?:gif|png))/i;
export const SHARED_IMAGE = /(?:^|[/_\-])(logo|sprite|banner|share|sharing|og[_-]?default|og[_-]?image|twitter[_-]?card|default|noimage|no[_-]?image|dummy|avatar|flag|icon|favicon|watermark|brand)(?:[/_\-.]|$)/i;
export const JUNK_TITLES = /^(link|links|watch|watch now|play|play now|view|view now|download|download now|read more|more|click here|click|open|open now|see more|show more|go|next|prev|previous|video|photo|image|here|→|»|›|▶|►|•|-|--)$/i;
const VIDEO_HINT_ATTRS = ["data-poster", "data-video-poster", "data-video-thumb", "data-video-preview", "data-preview", "data-preview-src", "data-video", "data-video-src", "data-video-id", "data-video-url", "data-src-video", "poster"];
const PHOTO_PATHS = ["/photo/", "/photos/", "/image/", "/images/", "/img/", "/album/", "/albums/", "/gallery/", "/galleries/", "/pic/", "/pics/", "/picture/", "/pictures/", "/image-gallery/", "/photo-gallery/"];
const VIDEO_PATHS = ["/video/", "/videos/", "/watch/", "/play/", "/player/", "/embed/", "/v/", "/clip/", "/clips/", "/movie/", "/movies/", "/stream/", "/tube/", "/p/", "/content_video/", "/content_video_alt/"];
const SKIP_PATHS = ["/login", "/signup", "/register", "/terms", "/privacy", "/contact", "/about", "/faq", "/dmca"];
const NAV_CLASSES = ["navbar", "nav-", "-nav", "menu", "header", "footer", "breadcrumb", "pagination"];
export const DUR_RE = /(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)/;
export const NUM_RE = /\d[\d.,]*\s*[kKmMbB]?(?![a-zA-Z])/;
export const PCT_RE = /(\d{1,3}(?:\.\d+)?)\s*%/;
const LAZY_ATTRS = ["data-src", "data-original", "data-lazy", "data-lazy-src", "data-thumb", "data-thumbnail", "data-image", "data-cover", "data-poster", "data-preview", "data-webp", "data-src-retina"];

// ------------------------------------------------------------------ small helpers
export const absUrl = (base: string, u?: string | null): string | null => {
  if (!u) return null;
  u = u.trim().replace(/^['"]|['"]$/g, "");
  if (!u) return null;
  try { return new URL(u, base).toString(); } catch { return null; }
};
export const attr = (el: HTMLElement | null | undefined, n: string): string => (el ? el.getAttribute(n) || "" : "");
export const clsOf = (el: HTMLElement): string => (el.getAttribute("class") || "").toLowerCase();
export const collapse = (s: string) => s.replace(/\s+/g, " ").trim();
export const textOf = (el: HTMLElement | null | undefined) => (el ? collapse(el.text || "") : "");
export const noQuery = (u: string) => u.split("?")[0];

export function hash12(s: string): string {
  let h1 = 0xdeadbeef ^ s.length, h2 = 0x41c6ce57 ^ s.length;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return ((h2 >>> 0).toString(16).padStart(8, "0") + (h1 >>> 0).toString(16).padStart(8, "0")).slice(0, 12);
}

export const isVideoUrl = (u?: string | null) => !!u && !IMAGE_EXT.test(u.toLowerCase().split("?")[0]) && VIDEO_EXT.test(u.toLowerCase().split("?")[0]);
const acceptableThumb = (u: string | null) => !!u && !isVideoUrl(u) && !PLACEHOLDER.test(u) && !SHARED_IMAGE.test(u);

export function pickFromSrcset(srcset: string, base: string): string | null {
  if (!srcset) return null;
  let best: string | null = null, bestW = -1;
  for (const part of srcset.split(",")) {
    const bits = part.trim().split(/\s+/);
    if (!bits[0]) continue;
    let w = 0;
    const m = bits[1] && bits[1].match(/^(\d+(?:\.\d+)?)([wx])/);
    if (m) w = Math.round(parseFloat(m[1]) * (m[2] === "x" ? 1000 : 1));
    if (w > bestW) { best = bits[0]; bestW = w; }
  }
  return absUrl(base, best);
}

export function findThumbnail(c: HTMLElement, base: string): string | null {
  const ok = (u: string | null) => (acceptableThumb(u) ? u : null);
  for (const v of c.querySelectorAll("video")) {
    for (const a of ["poster", "data-poster", "data-video-poster"]) { const h = ok(absUrl(base, attr(v, a))); if (h) return h; }
  }
  for (const img of c.querySelectorAll("img")) {
    for (const a of LAZY_ATTRS) { const h = ok(absUrl(base, attr(img, a))); if (h) return h; }
    for (const a of ["data-srcset", "srcset"]) { const h = ok(pickFromSrcset(attr(img, a), base)); if (h) return h; }
    const h = ok(absUrl(base, attr(img, "src")));
    if (h) return h;
  }
  for (const s of c.querySelectorAll("source")) {
    for (const a of ["data-srcset", "srcset"]) { const h = ok(pickFromSrcset(attr(s, a), base)); if (h) return h; }
    const h = ok(absUrl(base, attr(s, "data-src") || attr(s, "src")));
    if (h) return h;
  }
  for (const el of [c, ...c.querySelectorAll("[style]")]) {
    const m = attr(el, "style").match(/url\(\s*['"]?([^'")]+)['"]?\s*\)/);
    if (m) { const h = ok(absUrl(base, m[1])); if (h) return h; }
  }
  for (const el of [c, ...c.querySelectorAll("[data-bg],[data-background],[data-bg-src],[data-cover]")]) {
    for (const a of ["data-bg", "data-background", "data-bg-src", "data-cover"]) { const h = ok(absUrl(base, attr(el, a))); if (h) return h; }
  }
  return null;
}

function findVideoSource(c: HTMLElement, base: string): string | null {
  for (const v of c.querySelectorAll("video")) {
    const src = attr(v, "src") || attr(v, "data-src");
    if (isVideoUrl(src)) return absUrl(base, src);
    for (const s of v.querySelectorAll("source")) {
      const ss = attr(s, "src") || attr(s, "data-src");
      if (isVideoUrl(ss)) return absUrl(base, ss);
    }
  }
  return null;
}

function cleanTitle(t?: string | null): string | null {
  if (!t) return null;
  let s = t.replace(/<\/?[A-Za-z][^<>]{0,200}>/g, " ").replace(/\u00a0/g, " ");
  s = collapse(s);
  if (!s || JUNK_TITLES.test(s) || s.length <= 2) return null;
  return s;
}

export function findTitle(c: HTMLElement): string | null {
  for (const a of ["title", "aria-label", "data-title", "data-name", "data-video-title", "data-original-title", "data-tooltip", "data-label"]) {
    const t = cleanTitle(attr(c, a));
    if (t) return t;
  }
  for (const tag of ["h1", "h2", "h3", "h4", "h5"]) {
    const h = c.querySelector(tag);
    if (h) { const t = cleanTitle(textOf(h)); if (t) return t; }
  }
  const img = c.querySelector("img");
  if (img) {
    const alt = attr(img, "alt").trim();
    if (alt && !PLACEHOLDER.test(alt)) { const t = cleanTitle(alt); if (t) return t; }
  }
  for (const cls of ["title", "name", "video-title", "card-title", "entry-title", "post-title", "item-title"]) {
    const el = c.querySelector(`[class*="${cls}"]`);
    if (el) { const t = cleanTitle(textOf(el)); if (t) return t; }
  }
  for (const el of c.querySelectorAll("span,div,p,strong,b")) {
    const t = cleanTitle(textOf(el));
    if (t && t.length > 3 && t.length < 300) return t;
  }
  return null;
}

export function titleFromUrl(u: string): string | null {
  try {
    let slug = new URL(u).pathname.replace(/\/+$/, "").split("/").pop() || "";
    slug = slug.replace(/\.(html?|php|aspx?)$/i, "").replace(/[-_]+/g, " ").trim();
    return slug ? slug.replace(/\b\w/g, (m) => m.toUpperCase()) : null;
  } catch { return null; }
}

const HINT_SEL = VIDEO_HINT_ATTRS.map((a) => `[${a}]`).join(",");
function cardHasVideoHint(a: HTMLElement): boolean {
  if (a.querySelector("video")) return true;
  if (VIDEO_HINT_ATTRS.some((n) => attr(a, n))) return true;
  return !!a.querySelector(HINT_SEL);
}

function isVideoPreview(a: HTMLElement, baseHost: string, base: string): boolean {
  const href = attr(a, "href").trim();
  if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) return false;
  let u: URL;
  try { u = new URL(href, base); } catch { return false; }
  if (baseHost && !u.host.includes(baseHost)) return false;
  const path = u.pathname || "/";
  const low = path.toLowerCase();
  if (PHOTO_PATHS.some((f) => low.includes(f))) return false;
  if (SKIP_PATHS.some((s) => low.startsWith(s))) return false;
  const cid = (clsOf(a) + " " + attr(a, "id").toLowerCase());
  if (NAV_CLASSES.some((k) => cid.includes(k))) return false;
  if (cardHasVideoHint(a)) return true;
  return VIDEO_PATHS.some((f) => low.includes(f)) || VIDEO_EXT.test(low);
}

// ------------------------------------------------------------------ paging
export function currentPageNumber(url: string): number {
  let u: URL;
  try { u = new URL(url); } catch { return 1; }
  for (const k of ["page", "p", "paged", "pg"]) {
    const v = u.searchParams.get(k);
    if (v !== null && /^\d+$/.test(v)) return parseInt(v, 10);
  }
  let m = u.pathname.match(/\/page\/(\d+)/);
  if (m) return parseInt(m[1], 10);
  m = u.pathname.match(/\/(\d+)\/?$/);
  if (m) return parseInt(m[1], 10);
  return 1;
}

export function guessNextPage(base: string, pageNum: number): string {
  const nxt = pageNum + 1;
  const u = new URL(base);
  for (const k of ["page", "p", "paged", "pg"]) {
    if (u.searchParams.has(k)) { u.searchParams.set(k, String(nxt)); u.hash = ""; return u.toString(); }
  }
  if (/\/page\/\d+\/?/.test(u.pathname)) { u.pathname = u.pathname.replace(/\/page\/\d+\/?/, `/page/${nxt}/`); u.hash = ""; return u.toString(); }
  if (/\/\d+\/?$/.test(u.pathname)) { u.pathname = u.pathname.replace(/\/\d+\/?$/, `/${nxt}/`); return u.toString(); }
  u.searchParams.set("page", String(nxt));
  return u.toString();
}

export function findNextPageLink(root: HTMLElement, base: string, baseHost: string, anchors: HTMLElement[]): string | null {
  const rel = root.querySelector('link[rel="next"]') || root.querySelector('a[rel="next"]');
  if (rel && attr(rel, "href")) return absUrl(base, attr(rel, "href"));
  const cur = currentPageNumber(base);
  let bestN = Infinity, bestUrl: string | null = null;
  for (const a of anchors) {
    const t = textOf(a);
    if (!/^\d+$/.test(t)) continue;
    const n = parseInt(t, 10);
    if (n <= cur || n >= bestN) continue;
    const full = absUrl(base, attr(a, "href"));
    if (!full) continue;
    try { if (baseHost && !new URL(full).host.includes(baseHost)) continue; } catch { continue; }
    bestN = n; bestUrl = full;
  }
  if (bestUrl) return bestUrl;
  for (const a of anchors) {
    const blob = `${textOf(a)} ${attr(a, "aria-label")} ${attr(a, "rel")} ${clsOf(a)} ${attr(a, "title")}`.toLowerCase();
    if (!/(next|older|→|»|›)/.test(blob)) continue;
    const href = attr(a, "href");
    if (!href || /^(#|javascript:|mailto:)/i.test(href)) continue;
    const full = absUrl(base, href);
    if (!full) continue;
    try { if (baseHost && !new URL(full).host.includes(baseHost)) continue; } catch { continue; }
    return full;
  }
  return null;
}

// ------------------------------------------------------------------ card metadata
function cardMeta(card: HTMLElement): Partial<Item> {
  const meta: Partial<Item> = {};
  const first = (sels: string[]) => {
    for (const s of sels) { const el = card.querySelector(s); if (el) { const t = textOf(el); if (t) return t; } }
    return "";
  };
  const dur = first([".duracion", ".duration", '[class*="duration"]', '[class*="length"]', "time", '[class*="time"]']);
  let m = dur.match(DUR_RE);
  if (!m) {
    for (const el of card.querySelectorAll("span,div,em,i,b").slice(0, 40)) {
      const t = textOf(el);
      if (t.length <= 14 && DUR_RE.test(t) && !el.querySelector("a")) { m = t.match(DUR_RE); break; }
    }
  }
  if (m) meta.duration = m[1];
  const v = first([".thumb-video-views", ".views", '[class*="views"]', '[class*="view-count"]']);
  const mv = v.match(NUM_RE);
  if (mv) meta.views = mv[0].replace(/\s+/g, "");
  const r = first([".rating", '[class*="rating"]', '[class*="likes"]', '[class*="percent"]']);
  const mr = r.match(PCT_RE);
  if (mr) meta.rating = mr[1] + "%";
  let d = first(['[class*="added"]', '[class*="date"]', '[class*="ago"]']);
  const tm = card.querySelector("time");
  if (tm && (attr(tm, "datetime") || textOf(tm))) d = textOf(tm) || attr(tm, "datetime");
  if (d && d.length <= 30 && !/^(\d{1,2}:\d{2}(?::\d{2})?)$/.test(d)) meta.added = d;
  const q = first([".hd", ".is-hd", '[class*="quality"]', '[class*="resolution"]']);
  if (q && q.length <= 8) meta.quality = q.toUpperCase();
  return meta;
}

// ------------------------------------------------------------------ page parsing
export function prepare(html: string): HTMLElement {
  let h = html.length > MAX_HTML ? html.slice(0, MAX_HTML) : html;
  h = h.replace(/<script\b[\s\S]*?<\/script\s*>/gi, "").replace(/<style\b[\s\S]*?<\/style\s*>/gi, "").replace(/<!--[\s\S]*?-->/g, "");
  return parse(h, { blockTextElements: { script: false, style: false, noscript: false, pre: true } });
}

export function extractItems(root: HTMLElement, anchors: HTMLElement[], base: string, maxItems = 80): Item[] {
  const baseHost = new URL(base).host;
  const baseNoSlash = base.replace(/\/+$/, "");
  const picked: { a: HTMLElement; full: string; key: string }[] = [];
  const seen = new Set<string>();
  for (const a of anchors) {
    if (!isVideoPreview(a, baseHost, base)) continue;
    const abs = absUrl(base, attr(a, "href"));
    if (!abs) continue;
    const full = abs.split("#")[0];
    if (full.replace(/\/+$/, "") === baseNoSlash) continue;
    const key = noQuery(full);
    if (seen.has(key)) continue;
    seen.add(key);
    picked.push({ a, full, key });
    if (picked.length >= maxItems) break;
  }

  // card container in one pass: climb <=6 levels from every video link, remember which links each ancestor holds
  const holds = new Map<HTMLElement, Set<string>>();
  for (const p of picked) {
    let node = p.a.parentNode as HTMLElement | null;
    for (let i = 0; i < 6 && node && node.tagName && !["BODY", "HTML"].includes(node.tagName.toUpperCase()); i++) {
      let s = holds.get(node);
      if (!s) { s = new Set(); holds.set(node, s); }
      s.add(p.key);
      node = node.parentNode as HTMLElement | null;
    }
  }
  const cardOf = (a: HTMLElement): HTMLElement => {
    let best = a;
    let node = a.parentNode as HTMLElement | null;
    for (let i = 0; i < 6 && node && node.tagName; i++) {
      const s = holds.get(node);
      if (!s || s.size > 1) break;
      best = node;
      node = node.parentNode as HTMLElement | null;
    }
    return best;
  };

  const items: Item[] = picked.map((p) => {
    const card = cardOf(p.a);
    const item: Item = {
      title: findTitle(p.a) || findTitle(card) || titleFromUrl(p.full) || p.full,
      thumbnail: findThumbnail(p.a, base) || (card !== p.a ? findThumbnail(card, base) : null),
      video_src: findVideoSource(p.a, base),
      link: p.full,
      page: base,
      uid: hash12(p.full),
    };
    try { Object.assign(item, cardMeta(card)); } catch { /* metadata is a bonus */ }
    return item;
  });

  // a thumbnail shared by 3+ cards is a placeholder/logo, not a real preview
  const counts = new Map<string, number>();
  for (const it of items) if (it.thumbnail) counts.set(it.thumbnail, (counts.get(it.thumbnail) || 0) + 1);
  for (const it of items) if (it.thumbnail && (counts.get(it.thumbnail) || 0) >= 3) it.thumbnail = null;
  return items;
}

// ---- politeness + resilience for every outgoing fetch --------------------------------------------
// Workers only keep ~6 connections open at once; extra fetches would stall, so queue them ourselves.
let active = 0;
const waiters: (() => void)[] = [];
async function acquire() {
  if (active < 6) { active++; return; }
  await new Promise<void>((r) => waiters.push(r));       // slot is handed over by release(), `active` stays as is
}
function release() { const w = waiters.shift(); if (w) w(); else active--; }
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function isBlockedHost(host: string): boolean {
  const h = host.toLowerCase();
  return h === "localhost" || h.endsWith(".local") || h.endsWith(".internal") ||
    /^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|0\.)/.test(h) || h.includes(":");
}

const looksLikeChallenge = (status: number, headers: Headers, body: string) =>
  headers.get("cf-mitigated") === "challenge" ||
  ((status === 403 || status === 503) && /just a moment|cf-chl|attention required|enable javascript and cookies/i.test(body.slice(0, 4000)));

export async function fetchHtml(url: string, timeoutMs = 20000, signal?: AbortSignal, referer?: string) {
  const u0 = new URL(url);
  if (!/^https?:$/.test(u0.protocol) || isBlockedHost(u0.hostname)) throw new Error("blocked url");
  let lastErr: unknown = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (signal?.aborted) throw new Error("aborted");
    await acquire();
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    const onAbort = () => ac.abort();
    signal?.addEventListener("abort", onAbort);
    try {
      const r = await fetch(url, {
        headers: {
          "User-Agent": UA,
          Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.9",
          ...(referer ? { Referer: referer } : {}),
        },
        redirect: "follow",
        signal: ac.signal,
      });
      const text = await r.text();
      if (looksLikeChallenge(r.status, r.headers, text)) throw new Error("blocked by bot challenge");
      if (!r.ok) {
        const err = new Error(`HTTP ${r.status}`);
        if (r.status === 429 || r.status >= 500) { lastErr = err; throw Object.assign(err, { retry: true }); }
        throw err;
      }
      if (new URL(r.url || url).hostname && isBlockedHost(new URL(r.url || url).hostname)) throw new Error("blocked redirect");
      return { html: text, final: r.url || url };
    } catch (e: any) {
      lastErr = e;
      const retryable = e?.retry || /network|fetch failed|connection/i.test(String(e?.message));
      if (!retryable || attempt === 1 || signal?.aborted) throw e;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      release();
    }
    await sleep(250 + Math.random() * 450);                // jitter before the single retry
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

export async function scrapePage(url: string, maxItems = 80, pageNum: number | null = null, timeoutMs = 20000, signal?: AbortSignal): Promise<PageResult> {
  try {
    const { html, final: base } = await fetchHtml(url, timeoutMs, signal);
    const root = prepare(html);
    const anchors = root.querySelectorAll("a[href]");
    const baseHost = new URL(base).host;
    const items = extractItems(root, anchors, base, maxItems);
    let next = findNextPageLink(root, base, baseHost, anchors);
    const explicit = !!next;
    const pn = pageNum ?? currentPageNumber(base);
    if (!next && items.length) next = guessNextPage(base, pn);
    return {
      page: base,
      page_title: collapse(root.querySelector("title")?.text || "") || base,
      items,
      count: items.length,
      next_page: next,
      next_is_guess: !explicit && !!next,
      page_num: pageNum ?? currentPageNumber(base),
    };
  } catch (e) {
    return { page: url, items: [], count: 0, error: e instanceof Error ? e.message : String(e), next_page: null, page_num: pageNum };
  }
}

// ------------------------------------------------------------------ search
export function buildSearchUrls(site: string, query: string): string[] {
  let s = site.replace(/\/+$/, "");
  if (!/^https?:/i.test(s)) s = "https://" + s;
  const q = encodeURIComponent(query).replace(/%20/g, "+");
  return [`${s}/?s=${q}`, `${s}/search?q=${q}`, `${s}/search/${q}`, `${s}/search/${q}/`, `${s}/?q=${q}`, `${s}/videos/search?q=${q}`];
}

/** Races the URL shapes like search_one(): the lowest-index shape that returns videos wins. */
export async function searchOne(
  site: string, query: string, maxItems = 40, preferred: number | null = null
): Promise<{ result: PageResult & { query: string; site: string; search_url: string; source: string }; winner: number | null }> {
  const urls = buildSearchUrls(site, query);
  const ac = new AbortController();
  const results: (PageResult | null)[] = urls.map(() => null);
  const run = async (i: number) => {
    const r = await scrapePage(urls[i], maxItems, null, 7000, ac.signal);
    results[i] = r;
    return r;
  };
  let hit = false;
  if (preferred !== null && preferred >= 0 && preferred < urls.length) {
    hit = !!(await run(preferred)).items.length;
  }
  if (!hit) {
    const idx = urls.map((_, i) => i).filter((i) => results[i] === null);
    await new Promise<void>((resolve) => {
      let pending = idx.length;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const check = () => {
        const best = results.findIndex((r) => r && r.items.length);
        if (best >= 0) {
          if (!results.slice(0, best).some((r) => r === null)) return resolve();
          if (!timer) timer = setTimeout(resolve, 600);
        }
        if (pending === 0) resolve();
      };
      for (const i of idx) run(i).then(() => { pending--; check(); });
    });
    ac.abort();                                   // stop the shapes that are still running
    if (hit === false) { /* keep results as collected */ }
  }
  const winner = results.findIndex((r) => r && r.items.length);
  const out = (winner >= 0 ? results[winner] : results.find((r) => r) || results[0]) as PageResult;
  const searchUrl = urls[winner >= 0 ? winner : Math.max(0, results.indexOf(out))];
  const next = out.items.length && !out.next_page ? guessNextPage(searchUrl, out.page_num || 1) : out.next_page;
  return {
    result: { ...out, next_page: next, query, site, search_url: searchUrl, source: out.items.length ? "site-search" : "none" },
    winner: winner >= 0 ? winner : null,
  };
}

/** Same cross-site ranking Flask's /api/search applies. */
export function scoreItem(it: Item, query: string): number {
  let s = 0;
  if (it.thumbnail) s += 10;
  if (it.duration) s += 4;
  if (it.views) s += 2;
  if (it.rating) s += 1;
  const t = (it.title || "").toLowerCase(), q = query.toLowerCase();
  if (t.startsWith(q)) s += 8;
  else if (t.includes(q)) s += 4;
  return s;
}

export function combineResults(results: any[], query: string) {
  const combined: any[] = [], seen = new Set<string>();
  for (const r of results) {
    const site = r.site || r.page || "";
    for (const it of r.items || []) {
      const k = noQuery(it.link || "");
      if (!k || seen.has(k)) continue;
      seen.add(k);
      combined.push({ ...it, _site: site, _score: scoreItem(it, query) });
    }
  }
  combined.sort((a, b) => b._score - a._score);
  return combined;
}

/** Parse WITHOUT stripping scripts (JSON-LD, inline player config). Used by resolve + metadata. */
export function prepareFull(html: string): HTMLElement {
  const h = html.length > MAX_HTML ? html.slice(0, MAX_HTML) : html;
  return parse(h.replace(/<!--[\s\S]*?-->/g, ""), { blockTextElements: { script: true, style: false, noscript: false, pre: true } });
}
export { UA };
