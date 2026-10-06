// ---------------------------------------------------------------------------
// Live listing scrapers (categories / pornstars / networks+studios / series / tags).
// TypeScript port of scrape_categories, scrape_models, scrape_studio_sections, scrape_tags and
// scrape_superporn_categories from scraper.py, with the same mode dispatch as Flask's /api/scrape-categories.
// ---------------------------------------------------------------------------
import { parse, HTMLElement } from "node-html-parser";
import {
  absUrl, attr, clsOf, collapse, textOf, noQuery, hash12, pickFromSrcset, findThumbnail, findTitle, titleFromUrl,
  findNextPageLink, currentPageNumber, guessNextPage, fetchHtml, prepare, JUNK_TITLES, DUR_RE, NUM_RE, PCT_RE,
} from "./scrape";

const SKIP_PATH_PREFIXES = ["/login", "/signup", "/register", "/terms", "/privacy", "/contact", "/about", "/faq", "/dmca", "/page/"];
const SKIP_CLASS_HINTS = ["navbar", "nav-", "-nav", "menu", "header", "footer", "breadcrumb", "pagination"];
const CARD_TOKENS = new Set(["channel", "channels", "categories_card", "category-card", "category_card", "item", "card", "thumb", "studio", "model", "site-card", "sponsor", "thumb-serie", "categories_card_body"]);
const CARD_SUBSTR = ["categories_card", "category-card", "category_card", "site-card", "thumb-serie"];
const PATH_OK = /\/(?:categor(?:y|ies)|cat|tags?|c|video-category|studio|studios|channel|channels|model|models|site|sites|network|networks|series|sponsor|pornstar)\//;
const STUDIO_PATH = /\/(?:sites?|studios?|channels?|networks?|sponsors?)\/[^/?#]+/i;
const SITE_SECTIONS = ["sites", "site", "networks", "network", "studios", "studio", "channels", "channel"];
const SP_SKIP = new Set(["categories", "series", "pornstars", "login", "signup", "upload", "contact", "tos", "dmca", "cookies-policy", "search", "live", "es", "de", "it", "fr", "br", "nl"]);
const COUNT_TAIL = /([\d][\d,.]*)\s*(?:videos?|vids?|clips?)\s*$/i;
const MODEL_HREF = /\/(?:models?|pornstars?|actresses)\/([^/?#]+)\/?$/i;

const parentOf = (el: HTMLElement): HTMLElement | null => (el.parentNode as HTMLElement | null) || null;
const titleOf = (root: HTMLElement) => collapse(root.querySelector("title")?.text || "");
const hasMedia = (el: HTMLElement) => !!(el.querySelector("img") || el.querySelector("video"));
const same = (a: string, b: string) => a.replace(/\/+$/, "") === b.replace(/\/+$/, "");
const countOf = (t: string): number | null => { const m = (t || "").match(/([\d,]+)/); return m ? parseInt(m[1].replace(/,/g, ""), 10) : null; };

function cleanCategoryName(text?: string | null): string | null {
  if (!text) return null;
  let t = collapse(text).replace(/[\s\n]*[\d,]+\s*(?:videos?|vids?|clips?)?\s*$/i, "").trim();
  if (!t || t.length < 2 || t.length > 80 || /^\d+$/.test(t) || JUNK_TITLES.test(t) || /\blogo\b/i.test(t)) return null;
  return t;
}

function imgUrl(img: HTMLElement | null | undefined, base: string): string | null {
  if (!img) return null;
  for (const a of ["data-src", "data-original", "data-lazy-src", "data-webp", "data-thumb", "data-savepage-src", "src"]) {
    const v = attr(img, a).trim();
    if (v && !v.startsWith("data:")) return absUrl(base, v);
  }
  for (const a of ["data-srcset", "srcset"]) {
    const g = pickFromSrcset(attr(img, a), base);
    if (g) return g;
  }
  return null;
}

// ------------------------------------------------------------------ categories (generic)
function cardForLink(a: HTMLElement): HTMLElement {
  const cands: HTMLElement[] = [];
  let node = parentOf(a);
  for (let i = 0; i < 6 && node && node.tagName; i++) {
    const tokens = clsOf(node).split(/\s+/).filter(Boolean);
    const joined = tokens.join(" ");
    if (tokens.some((t) => CARD_TOKENS.has(t)) || CARD_SUBSTR.some((k) => joined.includes(k))) cands.push(node);
    node = parentOf(node);
  }
  for (const c of cands) if (hasMedia(c)) return c;
  return cands.length ? cands[cands.length - 1] : a;
}

function isCategoryLink(a: HTMLElement, baseHost: string, base: string): boolean {
  const href = attr(a, "href").trim();
  if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) return false;
  let u: URL;
  try { u = new URL(href, base); } catch { return false; }
  if (baseHost && !u.host.includes(baseHost)) return false;
  const path = (u.pathname || "/").toLowerCase();
  if (SKIP_PATH_PREFIXES.some((s) => path.startsWith(s))) return false;
  const card = cardForLink(a);
  const hasImage = hasMedia(a) || hasMedia(card);
  if (!hasImage && !PATH_OK.test(path)) return false;
  const cid = clsOf(a) + " " + attr(a, "id").toLowerCase();
  return !SKIP_CLASS_HINTS.some((k) => cid.includes(k));
}

export function extractCategories(root: HTMLElement, base: string, maxItems = 200): any[] {
  const baseHost = new URL(base).host;
  const best = new Map<string, any>();
  const order: string[] = [];
  for (const a of root.querySelectorAll("a[href]")) {
    if (!isCategoryLink(a, baseHost, base)) continue;
    const abs = absUrl(base, attr(a, "href"));
    if (!abs) continue;
    const full = abs.split("#")[0];
    if (same(full, base)) continue;
    const path = (new URL(full).pathname || "/").replace(/\/+$/, "").toLowerCase();
    if (["/categories", "/category", "/cats", "/sites"].includes(path)) continue;
    if (/^\/videos(\/\d+)?$/.test(path)) continue;   // "all videos" listing and its pages (/videos/, /videos/2/ ...) are not categories/studios
    if (/^\/[a-z]{2}(\/|$)/.test(path) && /\/(sites|categories|cats)$/.test(path)) continue;
    const key = noQuery(full);
    const name = cleanCategoryName(findTitle(a)) || cleanCategoryName(titleFromUrl(full));
    if (!name) continue;
    const card = cardForLink(a);
    const thumb = findThumbnail(a, base) || findThumbnail(card, base);
    const cand = { name, thumbnail: thumb, link: full, uid: hash12(full), _has: !!thumb };
    const prev = best.get(key);
    if (!prev) { best.set(key, cand); order.push(key); }
    else if (cand._has && !prev._has) best.set(key, cand);
    if (order.length >= maxItems * 3) break;
  }
  const withT: any[] = [], without: any[] = [];
  for (const k of order) { const it = best.get(k); const has = it._has; delete it._has; (has && it.thumbnail ? withT : without).push(it); }
  const raw = (withT.length ? withT : without).slice(0, maxItems);
  const counts = new Map<string, number>();
  for (const it of raw) if (it.thumbnail) counts.set(it.thumbnail, (counts.get(it.thumbnail) || 0) + 1);
  for (const it of raw) if (it.thumbnail && (counts.get(it.thumbnail) || 0) >= 6) it.thumbnail = null;
  return raw;
}

async function pageOrError<T>(url: string, timeout: number, empty: T): Promise<{ html: string; base: string } | (T & { error: string })> {
  try { const r = await fetchHtml(url, timeout); return { html: r.html, base: r.final }; }
  catch (e) { return { ...empty, error: e instanceof Error ? e.message : String(e) } as any; }
}

export async function scrapeCategories(url: string, pageNum: number | null = null): Promise<any> {
  if (isFpTagsUrl(url)) return scrapeTags(url, "porntags");
  const got: any = await pageOrError(url, 25000, { page: url, categories: [], count: 0, next_page: null, page_num: pageNum });
  if (got.error) return got;
  const root = prepare(got.html), base = got.base, host = new URL(base).host;
  const categories = extractCategories(root, base);
  let next = findNextPageLink(root, base, host, root.querySelectorAll("a[href]"));
  const explicit = !!next;
  const pn = pageNum ?? currentPageNumber(base);
  if (!next && categories.length) next = guessNextPage(base, pn);
  return { page: base, page_title: titleOf(root) || base, categories, count: categories.length, next_page: next, next_is_guess: !explicit && !!next, page_num: pageNum ?? currentPageNumber(base) };
}

// ------------------------------------------------------------------ tags / site names (A-Z lists)
function cleanTagName(text?: string | null): string | null {
  if (!text) return null;
  let t = collapse(text).replace(/\s*\(\s*[\d,.]+\s*\)\s*$/, "").replace(/\s+[\d,.]+\s*(?:videos?|vids?|clips?)\s*$/i, "").trim();
  return !t || t.length > 60 ? null : t;
}
const tagLetter = (n: string) => { const c = (n || "?")[0].toUpperCase(); return /[A-Z\u00C0-\u024F\u0400-\u04FF]/i.test(c) ? c : "#"; };

const CHROME_TAGS = new Set(["HEADER", "NAV", "FOOTER", "ASIDE", "SCRIPT", "STYLE", "NOSCRIPT"]);
const CHROME_ATTR = /(^|[\s_-])(lang|language|languages|locale|flags?|navbar|nav|menu|topbar|top-bar|header|footer|breadcrumbs?|sidebar|dropdown|pagination)([\s_-]|$)/i;
const LANG_HREF = /^\/(?:[a-z]{2}(?:-[a-z]{2})?)\/?$/i;
export function isChrome(el: HTMLElement): boolean {
  if (CHROME_TAGS.has((el.tagName || "").toUpperCase())) return true;
  if (["navigation", "banner", "contentinfo"].includes(attr(el, "role"))) return true;
  if ((el.tagName || "").toUpperCase() === "A") {
    if (attr(el, "hreflang") || LANG_HREF.test(attr(el, "href").trim())) return true;
  }
  const a = ((el.getAttribute("class") || "") + " " + attr(el, "id")).trim();
  return !!a && CHROME_ATTR.test(a);
}
function bodyOnly(html: string): HTMLElement {
  const work = prepare(html);                                   // fresh copy: we remove nodes from it
  for (const el of work.querySelectorAll("*")) { try { if (el.parentNode && isChrome(el)) el.remove(); } catch { /* ignore */ } }
  for (const sel of ["main", "[role=main]", "#content", "#main", ".content", ".main-content", ".list-categories", ".categories", ".categories-list", ".category-list", ".container"]) {
    const n = work.querySelector(sel);
    if (n && n.querySelector("a[href]") && n.querySelector("img")) return n;
  }
  return work.querySelector("body") || work;
}

function collectTagLinks(root: HTMLElement, base: string, baseHost: string) {
  const found: [string, string, string][] = [];
  for (const a of root.querySelectorAll("a[href]")) {
    const href = attr(a, "href").trim();
    if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) continue;
    const abs = absUrl(base, href);
    if (!abs) continue;
    const full = abs.split("#")[0];
    const p = new URL(full);
    if (baseHost && p.host !== baseHost) continue;
    const segs = (p.pathname || "/").split("/").filter(Boolean);
    if (segs.length < 2) continue;
    const name = cleanTagName(collapse(a.text || "") || attr(a, "title"));
    if (name) found.push([segs[0].toLowerCase(), full, name]);
  }
  return found;
}

function extractAzTags(root: HTMLElement, base: string, maxItems = 2000, sections: string[] = ["categories"]): any[] {
  let rows = root.querySelectorAll("#custom_list_categories_categories_list_items .list-categories__row");
  if (!rows.length) rows = root.querySelectorAll(".list-categories__row");
  const tags: any[] = [], seen = new Set<string>();
  for (const row of rows) {
    const letter = textOf(row.querySelector(".list-categories__row--letter")) || null;
    for (const a of row.querySelectorAll(".list-categories__row--list a[href]")) {
      const name = cleanTagName(collapse(a.text || "") || attr(a, "title"));
      if (!name) continue;
      const abs = absUrl(base, attr(a, "href").trim());
      if (!abs) continue;
      const full = abs.split("#")[0];
      const segs = new URL(full).pathname.split("/").filter(Boolean).map((x) => x.toLowerCase());
      if (segs.length < 2 || (!sections.includes(segs[0]) && !segs.slice(0, -1).some((x) => sections.includes(x)))) continue;
      const key = noQuery(full).replace(/\/+$/, "");
      if (seen.has(key)) continue;
      seen.add(key);
      tags.push({ name, link: full, thumbnail: null, letter: (letter || tagLetter(name)).toUpperCase().slice(0, 1) || "#", uid: hash12(full) });
      if (tags.length >= maxItems) return tags;
    }
  }
  return tags;
}

function extractTags(html: string, base: string, maxItems = 2000): any[] {
  const root = prepare(html);
  const exact = extractAzTags(root, base, maxItems);
  if (exact.length) return exact;
  const host = new URL(base).host;
  let found = collectTagLinks(bodyOnly(html), base, host);
  if (!found.length) found = collectTagLinks(root, base, host);
  if (!found.length) return [];
  const cnt = new Map<string, number>();
  for (const [seg] of found) cnt.set(seg, (cnt.get(seg) || 0) + 1);
  const top = [...cnt.entries()].sort((a, b) => b[1] - a[1])[0][0];
  const tags: any[] = [], seen = new Set<string>();
  for (const [seg, full, name] of found) {
    const key = noQuery(full).replace(/\/+$/, "");
    if (seg !== top || seen.has(key)) continue;
    seen.add(key);
    tags.push({ name, link: full, thumbnail: null, letter: tagLetter(name), uid: hash12(full) });
    if (tags.length >= maxItems) break;
  }
  return tags;
}

function extractSiteNames(html: string, base: string, maxItems = 2000): any[] {
  const root = prepare(html);
  const exact = extractAzTags(root, base, maxItems, SITE_SECTIONS);
  if (exact.length) return exact;
  const host = new URL(base).host;
  const out: any[] = [], seen = new Set<string>();
  for (const r of [bodyOnly(html), root]) {
    for (const [seg, full, name] of collectTagLinks(r, base, host)) {
      if (!SITE_SECTIONS.includes(seg)) continue;
      const key = noQuery(full).replace(/\/+$/, "");
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ name, link: full, thumbnail: null, letter: tagLetter(name), uid: hash12(full) });
      if (out.length >= maxItems) break;
    }
    if (out.length) break;
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

function isFpTagsUrl(url: string): boolean {
  try { const p = new URL(url); return p.host.toLowerCase().endsWith("fullporno.to") && /^(\/[a-z]{2})?\/categories$/.test(p.pathname.replace(/\/+$/, "").toLowerCase()); } catch { return false; }
}

export async function scrapeTags(url: string, kind = "porntags", maxItems = 2000, maxPages = 4): Promise<any> {
  const isSites = kind === "pornsites";
  const tags: any[] = [], seenKeys = new Set<string>(), seenPages = new Set<string>();
  let title: string | null = null, base = url, html = "", next: string | null = url, pages = 0;
  try {
    while (next && pages < (isSites ? maxPages : 1) && !seenPages.has(next)) {
      seenPages.add(next);
      const r = await fetchHtml(next, 20000);
      html = r.html; base = r.final;
      const root = prepare(html);
      if (title === null) title = titleOf(root) || base;
      const found = isSites ? extractSiteNames(html, base, maxItems) : extractTags(html, base, maxItems);
      for (const t of found) { const k = noQuery(t.link).replace(/\/+$/, ""); if (!seenKeys.has(k)) { seenKeys.add(k); tags.push(t); } }
      pages++;
      next = isSites && found.length ? findNextPageLink(root, base, new URL(base).host, root.querySelectorAll("a[href]")) : null;
    }
  } catch (e) {
    if (!tags.length) return { page: url, categories: [], count: 0, error: e instanceof Error ? e.message : String(e), next_page: null, page_num: 1 };
  }
  if (!tags.length) {
    return { page: base, page_title: title, categories: [], count: 0, next_page: null, page_num: 1,
      error: `${isSites ? "Site list" : "A-Z tag list"} not found in the page received (${html.length} bytes) - the site may be blocking the scraper.` };
  }
  const cut = tags.slice(0, maxItems);
  return { page: base, page_title: title, categories: cut, count: cut.length, next_page: null, next_is_guess: false, page_num: 1, kind };
}

// ------------------------------------------------------------------ studio / network sections
function studioVideo(item: HTMLElement, base: string): any | null {
  const a = item.querySelector("a[href]");
  if (!a) return null;
  const link = absUrl(base, attr(a, "href").split("#")[0]);
  if (!link) return null;
  let title = attr(a, "title").trim();
  if (!title) title = textOf(item.querySelector(".item-info .title, .title, strong"));
  const img = item.querySelector("img");
  if (!title && img) title = attr(img, "alt").trim();
  if (!title) title = titleFromUrl(link) || "";
  const duration = textOf(item.querySelector(".duration")) || null;
  const out: any = { title, link, thumbnail: imgUrl(img, base), duration, preview: attr(item.querySelector("[data-preview]"), "data-preview") || null };
  const dm = (duration || "").match(DUR_RE);
  if (dm) out.duration = dm[1];
  const vm = textOf(item.querySelector(".views")).match(NUM_RE);
  if (vm) out.views = vm[0].replace(/\s+/g, "");
  const rm = textOf(item.querySelector(".rating")).match(PCT_RE);
  if (rm) out.rating = rm[1] + "%";
  return out;
}

function superpornSeries(root: HTMLElement, base: string, max = 100): any[] {
  const out: any[] = [], seen = new Set<string>();
  let cards = root.querySelectorAll(".thumb-serie");
  if (!cards.length) cards = root.querySelectorAll("[class*='thumb-serie']");
  for (const card of cards) {
    const a = card.querySelector("a[href]");
    if (!a) continue;
    const href = attr(a, "href").trim();
    if (!href || /^(#|javascript:)/i.test(href)) continue;
    const link = absUrl(base, href.split("#")[0]);
    if (!link) continue;
    const key = noQuery(link).replace(/\/+$/, "");
    if (seen.has(key)) continue;
    const img = card.querySelector("img") || a.querySelector("img");
    const name = (img && cleanCategoryName(attr(img, "alt"))) || cleanCategoryName(attr(a, "title") || attr(a, "aria-label")) || cleanCategoryName(titleFromUrl(link));
    if (!name) continue;
    seen.add(key);
    out.push({ name, link, thumbnail: findThumbnail(card, base) || findThumbnail(a, base), video_count: null, videos: [] });
    if (out.length >= max) break;
  }
  return out;
}

function studioSections(root: HTMLElement, base: string, max = 100): any[] {
  const series = superpornSeries(root, base, max);
  if (series.length) return series;
  const container = root.querySelector("#list_content_sources_sponsors_list_items") || root.querySelector("[id^='list_content_sources']") || root.querySelector("main, #content, .content") || root;
  const sections: any[] = [], seen = new Set<string>();
  let current: any = null;
  for (const el of container.querySelectorAll(".headline, .list-videos")) {
    if (clsOf(el).split(/\s+/).includes("headline")) {
      current = null;
      const more = el.querySelector("a.more") || el.querySelectorAll("a[href]").find((x) => STUDIO_PATH.test(attr(x, "href")));
      if (!more || !attr(more, "href")) continue;
      const link = absUrl(base, attr(more, "href").split("#")[0]);
      if (!link || !STUDIO_PATH.test(new URL(link).pathname)) continue;
      const h = el.querySelector("h1,h2,h3,h4");
      let name = (h ? textOf(h) : "") || attr(el, "title");
      if (!name) name = cleanCategoryName(titleFromUrl(link)) || "";
      const key = noQuery(link).replace(/\/+$/, "");
      if (!name || seen.has(key)) continue;
      const label = textOf(more);
      seen.add(key);
      current = { name, link, video_count: countOf(label), see_more_label: label || "See all", videos: [], thumbnail: null };
      sections.push(current);
      if (sections.length >= max) break;
    } else if (current) {
      for (const item of el.querySelectorAll(".item")) {
        const v = studioVideo(item, base);
        if (v && current.videos.every((x: any) => x.link !== v.link)) current.videos.push(v);
      }
    }
  }
  for (const s of sections) s.thumbnail = (s.videos.find((v: any) => v.thumbnail) || {}).thumbnail || null;
  if (sections.length) return sections;
  for (const a of container.querySelectorAll("a[href]")) {
    const link = absUrl(base, attr(a, "href").split("#")[0]);
    if (!link) continue;
    const path = new URL(link).pathname;
    if (!STUDIO_PATH.test(path) || /^\/sites?\/\d*\/?$/.test(path)) continue;
    const key = noQuery(link).replace(/\/+$/, "");
    if (seen.has(key)) continue;
    const img = a.querySelector("img");
    const name = cleanCategoryName(attr(a, "title") || (img ? attr(img, "alt") : "") || collapse(a.text || "") || titleFromUrl(link));
    if (!name) continue;
    seen.add(key);
    sections.push({ name, link, thumbnail: imgUrl(img, base), video_count: countOf(textOf(a)), see_more_label: "See all", videos: [] });
    if (sections.length >= max) break;
  }
  return sections;
}

export async function scrapeStudioSections(url: string, max = 60): Promise<any> {
  const got: any = await pageOrError(url, 25000, { page: url, sections: [], count: 0, next_page: null, page_num: 1 });
  if (got.error) return got;
  const root = prepare(got.html), base = got.base;
  const sections = studioSections(root, base, max);
  let next = findNextPageLink(root, base, new URL(base).host, root.querySelectorAll("a[href]"));
  const explicit = !!next, pn = currentPageNumber(base);
  if (!next && sections.length) next = guessNextPage(base, pn);
  if (!sections.length) {
    return { page: base, page_title: titleOf(root), sections: [], count: 0, next_page: null, page_num: pn,
      error: `Studio sections not found in the page received (${got.html.length} bytes) - the site may be blocking the scraper.` };
  }
  return { page: base, page_title: titleOf(root) || base, sections, count: sections.length, next_page: next, next_is_guess: !explicit && !!next, page_num: pn, kind: "pornsites" };
}

// ------------------------------------------------------------------ superporn categories
function collapseRepeat(name: string): string {
  const w = (name || "").split(/\s+/).filter(Boolean), n = w.length;
  if (n >= 2 && n % 2 === 0 && w.slice(0, n / 2).join(" ").toLowerCase() === w.slice(n / 2).join(" ").toLowerCase()) return w.slice(0, n / 2).join(" ");
  return name;
}

function superpornCategories(root: HTMLElement, base: string, max = 200): any[] {
  const host = new URL(base).host;
  const out: any[] = [], seen = new Set<string>();
  for (const a of root.querySelectorAll("a[href]")) {
    const txt = collapse(a.text || "");
    const m = txt.match(COUNT_TAIL);
    if (!m || m.index === undefined) continue;
    const link = absUrl(base, attr(a, "href").split("#")[0]);
    if (!link) continue;
    const p = new URL(link);
    if (host && p.host !== host) continue;
    const slug = p.pathname.replace(/^\/+|\/+$/g, "");
    if (!slug || slug.includes("/") || SP_SKIP.has(slug.toLowerCase())) continue;
    const key = noQuery(link).replace(/\/+$/, "");
    if (seen.has(key)) continue;
    const img = a.querySelector("img");
    const name = cleanCategoryName(collapseRepeat(txt.slice(0, m.index).trim())) || cleanCategoryName(img ? attr(img, "alt") : null) || cleanCategoryName(titleFromUrl(link));
    if (!name) continue;
    seen.add(key);
    out.push({ name, link, thumbnail: imgUrl(img, base) || findThumbnail(a, base), video_count: countOf(m[1]), uid: hash12(link) });
    if (out.length >= max) break;
  }
  return out;
}

export async function scrapeSuperpornCategories(url: string, pageNum: number | null = null): Promise<any> {
  const got: any = await pageOrError(url, 25000, { page: url, categories: [], count: 0, next_page: null, page_num: pageNum || currentPageNumber(url), kind: "categories" });
  if (got.error) return got;
  const root = prepare(got.html), base = got.base;
  let cats = superpornCategories(root, base);
  if (!cats.length) cats = extractCategories(root, base);
  const pn = pageNum || currentPageNumber(base);
  let nxt = cats.length ? findNextPageLink(root, base, new URL(base).host, root.querySelectorAll("a[href]")) : null;
  if (nxt && currentPageNumber(nxt) <= pn) nxt = null;           // explicit pagination: never guess past the last page
  const out: any = { page: base, page_title: titleOf(root) || base, categories: cats, count: cats.length, next_page: nxt, next_is_guess: false, page_num: pn, kind: "categories" };
  if (!cats.length) out.error = `No categories found in the page received (${got.html.length} bytes).`;
  return out;
}

// ------------------------------------------------------------------ models / pornstars
function extractModels(root: HTMLElement, base: string, max = 200): any[] {
  const host = new URL(base).host;
  const out: any[] = [], seen = new Set<string>();
  const scope = root.querySelector("#list_models_models_list_items") || root.querySelector("[id^='list_models']") || root.querySelector(".list-models") || root;
  for (const a of scope.querySelectorAll("a[href]")) {
    const link = absUrl(base, attr(a, "href").split("#")[0]);
    if (!link) continue;
    const p = new URL(link);
    if (host && p.host !== host) continue;
    const mm = p.pathname.match(MODEL_HREF);
    if (!mm || /^\d+$/.test(mm[1])) continue;
    const key = noQuery(link).replace(/\/+$/, "");
    if (seen.has(key)) continue;
    let card = a;
    for (let i = 0; i < 3; i++) {
      if (card.querySelector("img") && ((card.tagName || "").toUpperCase() !== "A" || textOf(card))) break;
      const par = parentOf(card);
      if (!par || !par.tagName || ["BODY", "HTML"].includes(par.tagName.toUpperCase())) break;
      card = par;
    }
    const img = a.querySelector("img") || card.querySelector("img");
    const titleEl = card.querySelector(".title, strong, h2, h3");
    const name = cleanCategoryName(attr(a, "title") || (titleEl ? textOf(titleEl) : "") || (img ? attr(img, "alt") : "") || titleFromUrl(link));
    if (!name) continue;
    const cm = textOf(card).match(/([\d][\d,]*)\s*(?:videos?|vids?|clips?)/i);
    seen.add(key);
    out.push({ name, link, thumbnail: imgUrl(img, base), video_count: cm ? countOf(cm[1]) : null, uid: hash12(link) });
    if (out.length >= max) break;
  }
  return out;
}

export async function scrapeModels(url: string, pageNum: number | null = null): Promise<any> {
  const got: any = await pageOrError(url, 25000, { page: url, categories: [], models: [], count: 0, next_page: null, page_num: pageNum || currentPageNumber(url), kind: "pornstars" });
  if (got.error) return got;
  const root = prepare(got.html), base = got.base;
  const models = extractModels(root, base);
  const pn = pageNum || currentPageNumber(base);
  let nxt = models.length ? findNextPageLink(root, base, new URL(base).host, root.querySelectorAll("a[href]")) : null;
  const explicit = !!nxt;
  if (models.length && !nxt) nxt = guessNextPage(base, pn);
  const out: any = { page: base, page_title: titleOf(root) || base, categories: models, models, count: models.length, next_page: nxt, next_is_guess: !!nxt && !explicit, page_num: pn, kind: "pornstars" };
  if (!models.length) out.error = `No models found in the page received (${got.html.length} bytes).`;
  return out;
}

// ------------------------------------------------------------------ dispatcher = Flask /api/scrape-categories
export async function scrapeListing(url: string, mode: string | undefined, pageNum: number | null): Promise<any> {
  let host = "", path = "/";
  try { const p = new URL(url.includes("://") ? url : "https://" + url); host = p.hostname.toLowerCase(); path = (p.pathname || "/").toLowerCase(); } catch { /* keep */ }
  if (mode === "models" || mode === "pornstars" || (host.endsWith("freesexvideos.xxx") && path.startsWith("/models"))) return scrapeModels(url, pageNum);
  if (host.endsWith("superporn.com") && /^\/categories(\/\d+)?\/?$/.test(path)) return scrapeSuperpornCategories(url, pageNum);
  if (mode === "sites" || (host.endsWith("freesexvideos.xxx") && /^\/sites(\/\d+)?\/?$/.test(path))) return scrapeStudioSections(url);
  if (mode === "tags") return scrapeTags(url, "porntags");
  return scrapeCategories(url, pageNum);
}
