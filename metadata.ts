// ---------------------------------------------------------------------------
// Live video-page metadata (duration, date, views, rating + linked genres / stars / studios / tags).
// Port of extract_metadata() from metadata.py.
// ---------------------------------------------------------------------------
import { HTMLElement, TextNode } from "node-html-parser";
import { absUrl, attr, collapse, textOf, prepareFull, fetchHtml } from "./scrape";
import { isChrome } from "./listings";

const KIND_RX: [string, RegExp][] = [
  ["models", /\/(?:models?|pornstars?|porn-stars?|stars?|actors?|actresses?|girls?|performers?)\/[^/?#]+/i],
  ["studios", /\/(?:studios?|channels?|networks?|sites?|series|brands?|producers?|labels?)\/[^/?#]+/i],
  ["uploaders", /\/(?:users?|uploaders?|members?|profiles?)\/[^/?#]+/i],
  ["categories", /\/(?:categor(?:y|ies)|cats?|genres?|niches?)\/[^/?#]+/i],
  ["tags", /\/(?:tags?|keywords?|topics?)\/[^/?#]+/i],
];
const LABELS: Record<string, string> = { models: "Pornstars", studios: "Series / Studios", uploaders: "Uploader", categories: "Categories", tags: "Tags" };
const ORDER = ["models", "studios", "uploaders", "categories", "tags"];
const RESERVED = new Set(["login", "signup", "register", "categories", "category", "series", "pornstars", "pornstar", "models", "videos", "video", "search", "contact", "tos", "dmca", "terms", "privacy", "cookies-policy", "about", "faq", "upload", "live", "new", "popular", "trending", "latest", "best", "top", "favorites", "history", "help", "users", "user", "channels", "tags", "sitemap", "cookies", "legal", "explore", "community"]);
const JUNK_NAME = /^(view all|see all|show all|all|more|show more|load more|next|prev|previous|home|categories|category|tags?|models?|pornstars?|studios?|channels?|networks?|sites?|series|»|›|→|\.\.\.)$/i;
const BAD_SLUG = /^(page|p|\d+|feed|rss|all)$/i;
const LABEL_KIND: [string, RegExp][] = [
  ["models", /^\s*(?:porn\s*stars?|pornstars?|models?|actors?|actresses?|cast|stars?|performers?|girls?)\s*[:\uff1a]\s*$/i],
  ["studios", /^\s*(?:studios?|channels?|networks?|sites?|series|brands?|producers?|labels?|production)\s*[:\uff1a]\s*$/i],
  ["uploaders", /^\s*(?:uploaders?|uploaded\s+by|submitted\s+by|posted\s+by|users?|authors?|by)\s*[:\uff1a]\s*$/i],
  ["categories", /^\s*(?:categor(?:y|ies)|genres?|niches?|sections?)\s*[:\uff1a]\s*$/i],
  ["tags", /^\s*(?:tags?|keywords?|topics?)\s*[:\uff1a]\s*$/i],
];
const ANY_LABEL = /^\s*[^\W\d_][\w ]{1,24}\s*[:\uff1a]\s*$/u;

const unesc = (s: string) => (s || "").replace(/<\/?[A-Za-z][^<>]{0,200}>/g, " ").replace(/\u00a0/g, " ");
const fmtSeconds = (sec: number) => { const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60; return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`; };

function normDur(t: string): string | null {
  t = (t || "").trim();
  let m = t.match(/(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)/);
  if (m) return m[3] ? `${parseInt(m[1])}:${m[2]}:${m[3]}` : `${parseInt(m[1])}:${m[2]}`;
  const h = t.match(/(\d+)\s*(?:h|hr|hrs|hours?)\b/i), mi = t.match(/(\d+)\s*(?:m|min|mins|minutes?)\b/i), se = t.match(/(\d+)\s*(?:s|sec|secs|seconds?)\b/i);
  if (h || mi || se) {
    const tot = (h ? +h[1] * 3600 : 0) + (mi ? +mi[1] * 60 : 0) + (se ? +se[1] : 0);
    if (tot) return fmtSeconds(tot);
  }
  return null;
}
function toSeconds(v: any): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return v > 0 ? Math.floor(v) : null;
  const t = String(v).trim();
  const m = t.match(/^P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$/i);
  if (m && (m[1] || m[2] || m[3])) return Math.floor(+(m[1] || 0) * 3600 + +(m[2] || 0) * 60 + +(m[3] || 0)) || null;
  if (/^\d{1,6}(?:\.\d+)?$/.test(t)) return Math.floor(parseFloat(t)) || null;
  const d = normDur(t);
  if (d) return d.split(":").map(Number).reverse().reduce((a, p, i) => a + p * 60 ** i, 0) || null;
  return null;
}
function names(v: any): string[] {
  if (v === null || v === undefined) return [];
  if (typeof v === "string") return unesc(v).split(/[,;|]/).map((x) => x.trim()).filter(Boolean);
  if (Array.isArray(v)) return v.flatMap(names);
  if (typeof v === "object") return typeof v.name === "string" && v.name.trim() ? [v.name.trim()] : [];
  return [];
}
function cleanName(text?: string | null): string | null {
  let t = collapse(unesc(text || ""));
  t = t.replace(/\s*[(\[]\s*\d[\d,.\s]*[kKmM]?\s*[)\]]\s*$/, "");
  if (/[A-Za-z\u0400-\u04FF]\s+\d[\d,.]*[kKmM]?$/.test(t)) t = t.replace(/\s+\d[\d,.]*[kKmM]?$/, "");
  t = t.replace(/^[ ,;|\-–—•·#]+|[ ,;|\-–—•·#]+$/g, "");
  return !t || t.length > 45 || t.length < 2 || JUNK_NAME.test(t) ? null : t;
}

function jsonLd(root: HTMLElement): any {
  const nodes: any[] = [];
  for (const sc of root.querySelectorAll("script")) {
    if (!/ld\+json/i.test(attr(sc, "type"))) continue;
    const raw = (sc.rawText || "").trim();
    if (!raw) continue;
    let data: any;
    try { data = JSON.parse(raw); } catch { try { data = JSON.parse(raw.replace(/,\s*([}\]])/g, "$1")); } catch { continue; } }
    const stack = [data];
    while (stack.length) {
      const x = stack.pop();
      if (Array.isArray(x)) stack.push(...x);
      else if (x && typeof x === "object") { nodes.push(x); if (x["@graph"]) stack.push(x["@graph"]); }
    }
  }
  for (const n of nodes) {
    const t = Array.isArray(n["@type"]) ? n["@type"].join(" ") : String(n["@type"] || "");
    if (/VideoObject|Movie/i.test(t)) return n;
  }
  return {};
}

function metaVals(root: HTMLElement, ...ns: string[]): string[] {
  const out: string[] = [];
  for (const n of ns) for (const key of ["property", "name", "itemprop"]) for (const tag of root.querySelectorAll(`meta[${key}="${n}"]`)) { const c = attr(tag, "content").trim(); if (c) out.push(c); }
  return out;
}

type N = HTMLElement | TextNode;
const isEl = (n: N): n is HTMLElement => (n as any).nodeType === 1;
function flatten(root: HTMLElement): N[] {
  const out: N[] = [];
  const walk = (n: any) => { for (const c of n.childNodes || []) { out.push(c); if (c.nodeType === 1) walk(c); } };
  walk(root);
  return out;
}
const parentEl = (n: N): HTMLElement | null => ((n as any).parentNode as HTMLElement | null) || null;
const inside = (n: N | null, scope: HTMLElement): boolean => { while (n) { if (n === scope) return true; n = parentEl(n); } return false; };
const sameSite = (a: string, b: string) => { const reg = (u: string) => { try { return new URL(u).hostname.toLowerCase().split(".").slice(-2).join("."); } catch { return ""; } }; return reg(a) === reg(b); };

// Some sites (SPA-style pages) keep duration / stars / genres only in an embedded JSON state, not in the HTML.
// Find the object of THIS video there (matched by title) and read its fields; never guesses from other videos.
const EMB_LISTS: Record<string, string[]> = {
  models: ["pornstars", "pornStars", "performers", "models", "actors", "stars", "cast"],
  categories: ["categories", "genres", "niches", "category", "genre"],
  tags: ["tags", "keywords"],
  studios: ["channel", "studio", "network", "series", "site", "producer"],
};
const normTitle = (t: string) => (t || "").toLowerCase().replace(/[\W_]+/gu, " ").trim();
function embeddedVideo(root: HTMLElement, title: string | null): any | null {
  const want = normTitle(title || "");
  if (want.length < 4) return null;
  const roots: any[] = [];
  for (const sc of root.querySelectorAll("script")) {
    if (/ld\+json/i.test(attr(sc, "type"))) continue;
    const raw = (sc.rawText || "").trim();
    if (raw.length < 40 || raw.length > 3_000_000) continue;
    let txt: string | null = null;
    if (/^[\[{]/.test(raw)) txt = raw;
    else { const m = /(?:__[A-Z0-9_]+__|INITIAL_STATE|initialState)\s*=\s*([\[{][\s\S]*[\]}])\s*;?\s*$/.exec(raw); if (m) txt = m[1]; }
    if (!txt) continue;
    try { roots.push(JSON.parse(txt)); } catch { /* not json */ }
  }
  let n = 0;
  const stack: any[] = [...roots];
  while (stack.length && n++ < 30000) {
    const x = stack.pop();
    if (Array.isArray(x)) { for (const v of x) if (v && typeof v === "object") stack.push(v); continue; }
    if (!x || typeof x !== "object") continue;
    const t = x.title ?? x.name;
    if (typeof t === "string") { const tn = normTitle(t); if (tn === want || (tn.length > 8 && (want.includes(tn) || tn.includes(want)))) return x; }
    for (const v of Object.values(x)) if (v && typeof v === "object") stack.push(v);
  }
  return null;
}
const embNames = (v: any): string[] =>
  typeof v === "string" ? v.split(/[,;|]/).map((x) => x.trim()).filter(Boolean)
  : Array.isArray(v) ? v.flatMap((x) => (x && typeof x === "object" ? embNames(x.name ?? x.title ?? x.slug ?? "") : embNames(String(x ?? ""))))
  : v && typeof v === "object" ? embNames(v.name ?? v.title ?? v.slug ?? "") : [];

export function extractMetadata(html: string, url: string): any {
  const root = prepareFull(html);
  const ld = jsonLd(root);
  const chromeCache = new Map<HTMLElement, boolean>();
  const chrome = (el: HTMLElement) => { let v = chromeCache.get(el); if (v === undefined) { try { v = isChrome(el); } catch { v = false; } chromeCache.set(el, v); } return v; };
  const anchorOk = (a: HTMLElement) => {
    if (a.querySelector("img")) return false;
    if (chrome(a)) return false;
    let p = parentEl(a);
    while (p && p.tagName && !["BODY", "HTML"].includes(p.tagName.toUpperCase())) { if (chrome(p)) return false; p = parentEl(p); }
    return true;
  };
  const kindOf = (href: string, a: HTMLElement): string | null => {
    let pu: URL; try { pu = new URL(href); } catch { return null; }
    for (const [kind, rx] of KIND_RX) {
      const m = pu.pathname.match(rx);
      if (m) { const slug = m[0].replace(/\/+$/, "").split("/").pop() || ""; return BAD_SLUG.test(slug) ? null : kind; }
    }
    const rel = attr(a, "rel").toLowerCase();
    if (rel.includes("category")) return "categories";
    if (rel.includes("tag")) return "tags";
    const q = pu.search.slice(1).toLowerCase();
    if (/(?:^|&)(?:tag|tags)=/.test(q)) return "tags";
    if (/(?:^|&)(?:category|cat)=/.test(q)) return "categories";
    return null;
  };

  const flat = flatten(root);
  const index = new Map<N, number>();
  flat.forEach((n, i) => index.set(n, i));
  const allAnchors = root.querySelectorAll("a[href]");

  // --- title (the video's own <h1>, not the site header's)
  const h1s = root.querySelectorAll("h1");
  const norm = (t: string) => (t || "").replace(/[\W_]+/gu, " ").trim().toLowerCase();
  let h1: HTMLElement | null = h1s[0] || null;
  if (h1s.length > 1) {
    const ref = [norm(textOf(root.querySelector("title"))), ...metaVals(root, "og:title").map(norm)].join(" ");
    let best: HTMLElement | null = null, bl = 0;
    for (const h of h1s) { const t = norm(textOf(h)); if (t && ref.includes(t) && t.length > bl) { best = h; bl = t.length; } }
    h1 = best || h1s.reduce((a, b) => (textOf(b).length > textOf(a).length ? b : a));
  }

  const out: any = { url, title: null, duration: null, duration_seconds: null, date: null, views: null, rating: null, description: null, groups: [] };
  out.title = (typeof ld.name === "string" ? ld.name : null) || (h1 ? textOf(h1) : null) || metaVals(root, "og:title")[0] || null;
  if (out.title) out.title = collapse(unesc(out.title)) || null;

  let sec = toSeconds(ld.duration);
  if (!sec) for (const c of metaVals(root, "video:duration", "og:video:duration", "duration")) { sec = sec || toSeconds(c); }
  if (!sec) for (const el of root.querySelectorAll('[itemprop="duration"]')) { sec = toSeconds(attr(el, "content") || textOf(el)); if (sec) break; }
  if (!sec) {
    const scope = h1 && parentEl(h1) ? parentEl(h1)! : root;
    const text = collapse(scope.text || "").slice(0, 4000);
    const m = text.match(/(?:duration|length|runtime|время|длительность)\s*[:\-]?\s*((?:\d{1,2}:)?\d{1,2}:\d{2})/i);
    sec = m ? toSeconds(m[1]) : null;
  }
  if (sec) { out.duration_seconds = sec; out.duration = fmtSeconds(sec); }

  for (const c of [ld.uploadDate, ld.datePublished, ...metaVals(root, "video:release_date", "article:published_time", "uploadDate", "datePublished")]) {
    if (typeof c === "string" && /^\d{4}-\d{2}-\d{2}/.test(c)) { out.date = c.slice(0, 10); break; }
  }
  if (!out.date) { const t = root.querySelector("time[datetime]"); if (t && /^\d{4}-\d{2}-\d{2}/.test(attr(t, "datetime"))) out.date = attr(t, "datetime").slice(0, 10); }

  const stat = ld.interactionStatistic;
  const stats = Array.isArray(stat) ? stat : stat ? [stat] : [];
  for (const s of stats) {
    if (s && typeof s === "object" && s.userInteractionCount !== undefined && s.userInteractionCount !== null) {
      if (/Watch|View/i.test(JSON.stringify(s.interactionType || "")) || stats.length === 1) {
        const v = s.userInteractionCount;
        out.views = typeof v === "number" ? v.toLocaleString("en-US") : String(v).trim() || null;
        break;
      }
    }
  }
  if (!out.views && h1) {
    const start = index.get(h1) ?? 0;
    let seen = 0;
    for (let i = start + 1; i < flat.length && seen < 60; i++) {
      const n = flat[i];
      if (!isEl(n)) continue;
      seen++;
      if (/(views?|eye)/i.test(attr(n, "class"))) { const t = textOf(n); if (/^\d[\d\s,.]*\s?[kKmM]?$/.test(t)) { out.views = t; break; } }
    }
  }
  const ar = ld.aggregateRating;
  if (ar && typeof ar === "object" && ar.ratingValue !== undefined && ar.ratingValue !== null) out.rating = String(ar.ratingValue);
  const d = typeof ld.description === "string" ? ld.description : metaVals(root, "description", "og:description")[0];
  if (d) out.description = collapse(unesc(d)).slice(0, 400);

  // --- labeled rows ("Categories:", "Tags:", "Porn star:" ...)
  const labeled: Record<string, { name: string; link: string }[]> = {};
  for (let i = 0; i < flat.length; i++) {
    const ls = flat[i];
    if (isEl(ls)) continue;
    const txt = (ls as TextNode).text || "";
    if (!txt.includes(":") || txt.length > 40) continue;
    const t = txt.replace(/\s+/g, " ");
    const kind = (LABEL_KIND.find(([, rx]) => rx.test(t)) || [null])[0] as string | null;
    if (!kind) continue;
    let scope: HTMLElement | null = parentEl(ls);
    for (let k = 0; k < 3 && scope; k++) {
      let cnt = 0, hit = false;
      for (let j = i + 1; j < flat.length && cnt < 3; j++) { const n = flat[j]; if (isEl(n) && n.tagName === "A" && attr(n, "href")) { cnt++; if (inside(n, scope)) { hit = true; break; } } }
      if (hit) break;
      scope = parentEl(scope);
    }
    if (!scope || !scope.tagName || ["HTML", "BODY"].includes(scope.tagName.toUpperCase())) continue;
    const anchors: HTMLElement[] = [];
    for (let j = i + 1; j < flat.length; j++) {
      const n = flat[j];
      if (!inside(n, scope)) break;
      if (!isEl(n)) { if (ANY_LABEL.test((n as TextNode).text || "")) break; continue; }
      if (n.tagName === "A" && attr(n, "href")) anchors.push(n);
    }
    const seen = new Set((labeled[kind] || []).map((x) => x.link));
    for (const a of anchors) {
      const href = attr(a, "href").trim();
      if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) continue;
      const full = (absUrl(url, href) || "").split("#")[0];
      if (!full || seen.has(full) || !sameSite(full, url)) continue;
      const img = a.querySelector("img");
      const name = cleanName(textOf(a) || attr(a, "title") || (img ? attr(img, "alt") : ""));
      if (!name) continue;
      seen.add(full);
      (labeled[kind] = labeled[kind] || []).push({ name, link: full });
      if (labeled[kind].length >= 60) break;
    }
  }
  const hasLabeled = Object.keys(labeled).length > 0;

  // --- groups from link shapes when there are no labeled rows
  const linkedGroups = (): Record<string, any[]> => {
    const found: [HTMLElement, string, string][] = [];
    const bh = (() => { try { return new URL(url).host.replace("www.", ""); } catch { return ""; } })();
    for (const a of allAnchors) {
      const href = attr(a, "href").trim();
      if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) continue;
      const full = (absUrl(url, href) || "").split("#")[0];
      if (!full) continue;
      try { if (new URL(full).host.replace("www.", "") !== bh) continue; } catch { continue; }
      if (!anchorOk(a)) continue;
      const kind = kindOf(full, a);
      if (kind) found.push([a, full, kind]);
    }
    if (!found.length) return {};
    let pool = found;
    const h = root.querySelector("h1");
    if (h) {
      const ids = new Set(found.map((f) => f[0]));
      let node = parentEl(h);
      for (let i = 0; i < 6 && node && node.tagName && !["BODY", "HTML"].includes(node.tagName.toUpperCase()); i++) {
        const ins = node.querySelectorAll("a[href]").filter((x) => ids.has(x));
        if (ins.length >= 2) { const keep = new Set(ins); pool = found.filter((f) => keep.has(f[0])); break; }
        node = parentEl(node);
      }
    }
    const groups: Record<string, Map<string, any>> = {};
    for (const [a, full, kind] of pool) {
      const name = cleanName(textOf(a) ? textOf(a) : attr(a, "title"));
      if (!name) continue;
      const g = (groups[kind] = groups[kind] || new Map());
      const k = full.replace(/\/+$/, "");
      if (!g.has(k)) g.set(k, { name, link: full });
    }
    const res: Record<string, any[]> = {};
    for (const [k, v] of Object.entries(groups)) if (v.size > 0 && v.size <= 40) res[k] = [...v.values()];
    return res;
  };

  const groups: Record<string, any[]> = {};
  for (const [k, v] of Object.entries(hasLabeled ? labeled : linkedGroups())) groups[k] = [...v];

  const addNames = (kind: string, list: string[]) => {
    const have = new Set((groups[kind] || []).map((x) => x.name.toLowerCase()));
    for (const raw of list) {
      const n = cleanName(raw);
      if (!n || have.has(n.toLowerCase())) continue;
      have.add(n.toLowerCase());
      let entry: any = { name: n, link: null };
      const tags = groups.tags || [];
      const hit = tags.find((t) => t.name.toLowerCase() === n.toLowerCase() && t.link);
      if (hit && kind !== "tags") { entry = hit; tags.splice(tags.indexOf(hit), 1); if (!tags.length) delete groups.tags; }
      (groups[kind] = groups[kind] || []).push(entry);
    }
  };

  if (!hasLabeled && !(groups.tags || []).length && !(groups.categories || []).length && h1) {
    // flat tag row right after the title: /some-tag/ style links
    const host = (() => { try { return new URL(url).host.replace("www.", ""); } catch { return ""; } })();
    const run: { name: string; link: string }[] = [];
    let started = false, other = 0, seenA = 0;
    for (let i = (index.get(h1) ?? 0) + 1; i < flat.length && seenA < 140; i++) {
      const a = flat[i];
      if (!isEl(a) || a.tagName !== "A" || !attr(a, "href")) continue;
      seenA++;
      const full = (absUrl(url, attr(a, "href").trim()) || "").split("#")[0];
      let pu: URL; try { pu = new URL(full); } catch { continue; }
      const slug = pu.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
      const flatLink = pu.host.replace("www.", "") === host && !pu.search && /^\/[a-z0-9][a-z0-9-]{1,40}\/?$/i.test(pu.pathname || "") && !RESERVED.has(slug) && !a.querySelector("img") && anchorOk(a);
      if (flatLink) { const name = cleanName(textOf(a) || attr(a, "title")); if (name) { started = true; run.push({ name, link: full }); } continue; }
      if (started) break;
      if (++other > 12) break;
    }
    const seen = new Set<string>(), uniq = run.filter((x) => { const k = x.link.replace(/\/+$/, ""); if (seen.has(k)) return false; seen.add(k); return true; });
    if (uniq.length) groups.tags = uniq.slice(0, 20);
  }
  addNames("categories", [...names(ld.genre), ...metaVals(root, "article:section")]);
  addNames("models", [...names(ld.actor), ...names(ld.performer)]);
  addNames("studios", [...names(ld.productionCompany), ...names(ld.publisher)]);
  if (!(groups.tags || []).length) addNames("tags", [...names(ld.keywords), ...metaVals(root, "video:tag", "article:tag")]);
  if (!(groups.tags || []).length && !(groups.categories || []).length) addNames("tags", metaVals(root, "keywords").flatMap(names).slice(0, 20));

  // fill what the HTML did not give from the page's embedded JSON state (only this video's own object)
  const emb = embeddedVideo(root, out.title);
  if (emb) {
    const pick = (keys: string[]) => { for (const k of keys) if (emb[k] !== undefined && emb[k] !== null && emb[k] !== "") return emb[k]; return null; };
    if (!out.duration_seconds) { const s = toSeconds(pick(["duration", "durationSeconds", "duration_seconds", "length", "lengthSeconds", "runtime"])); if (s) { out.duration_seconds = s; out.duration = fmtSeconds(s); } }
    if (!out.views) { const v = pick(["views", "viewCount", "view_count", "viewsCount", "numViews"]); if (v !== null && typeof v !== "object") out.views = typeof v === "number" ? v.toLocaleString("en-US") : String(v).trim() || null; }
    if (!out.rating) { const r = pick(["rating", "ratingValue", "likesPercent", "likes_percent"]); if (r !== null && typeof r !== "object") out.rating = String(r); }
    if (!out.date) { const d = pick(["uploadDate", "createdAt", "created_at", "publishedAt", "published_at", "datePublished", "releaseDate", "added"]); if (typeof d === "string" && /^\d{4}-\d{2}-\d{2}/.test(d)) out.date = d.slice(0, 10); }
    for (const [kind, keys] of Object.entries(EMB_LISTS)) {
      if ((groups[kind] || []).length) continue;
      const k = keys.find((x) => emb[x] !== undefined && emb[x] !== null && emb[x] !== "");
      if (k) addNames(kind, embNames(emb[k]).slice(0, 30));
    }
  }

  // PornVideoBB shows the same words as "genre" and "tags": keep one list (Genres). Elsewhere, drop tags that repeat a genre.
  const host = (() => { try { return new URL(url).hostname.toLowerCase(); } catch { return ""; } })();
  if (host.includes("pornvideobb")) {
    if ((groups.categories || []).length) delete groups.tags;
    else if (groups.tags) { groups.categories = groups.tags; delete groups.tags; }
  } else if ((groups.tags || []).length && (groups.categories || []).length) {
    const cat = new Set(groups.categories.map((x) => x.name.toLowerCase()));
    groups.tags = groups.tags.filter((t) => !cat.has(t.name.toLowerCase()));
    if (!groups.tags.length) delete groups.tags;
  }

  out.groups = ORDER.filter((k) => (groups[k] || []).length).map((k) => ({ kind: k, label: LABELS[k], items: groups[k].slice(0, 40) }));
  return out;
}

export async function fetchMetadata(url: string): Promise<any> {
  try {
    const p = new URL(url);
    const { html, final } = await fetchHtml(url, 15000, undefined, `${p.protocol}//${p.host}/`);
    return extractMetadata(html, final);
  } catch (e) {
    return { url, groups: [], error: e instanceof Error ? e.message : String(e) };
  }
}
