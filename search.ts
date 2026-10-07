// ---------------------------------------------------------------------------
// "Search everything": the typed keyword is matched against title, tags, genres/categories, pornstars/models,
// studios/series, description and the URL slug - not only the title.
//
// Three sources feed one ranking (all built so a miss never means an empty page):
//   1. the sites' own search pages            (scrape.ts -> searchOne)
//   2. TAXONOMY pages                          a keyword that is a category / tag / star / studio name opens that
//                                              page's feed (`taxonomy-index` KV key, written by scraper_kv.py)
//   3. the local SEARCH INDEX                  `search-index` KV key: every video the scraper has seen, with the
//                                              tags/genres/stars/studios from its metadata. Zero network cost.
// ---------------------------------------------------------------------------
import { hash12, noQuery } from "./scrape";

// ------------------------------------------------------------------ text normalisation
export const norm = (s: string | null | undefined): string =>
  (s || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

/** words that say nothing about a video on an adult site; dropped only when real keywords remain */
const STOP = new Set(["the", "a", "an", "and", "or", "of", "in", "on", "with", "for", "to", "video", "videos", "porn", "free", "hd", "xxx", "full", "movie", "movies", "sex", "online"]);

/** crude plural / suffix folding: tits=tit, babes=babe, lesbians=lesbian, bitches=bitch, ladies=lady */
export function stem(w: string): string {
  if (w.length > 4 && w.endsWith("ies")) return w.slice(0, -3) + "y";
  if (w.length > 4 && /(ches|shes|sses|xes|zes)$/.test(w)) return w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss") && !w.endsWith("us")) return w.slice(0, -1);
  return w;
}
const stems = (s: string): string[] => norm(s).split(" ").filter(Boolean).map(stem);

export interface Query { raw: string; norm: string; stems: string[]; slug: string }

export function parseQuery(q: string): Query {
  const n = norm(q);
  const all = n.split(" ").filter(Boolean);
  const keep = all.filter((w) => !STOP.has(w));
  const words = (keep.length ? keep : all).slice(0, 8);
  return { raw: (q || "").trim(), norm: words.join(" "), stems: words.map(stem), slug: all.join("-") };
}

/** cache-key form of a query: identical on the Worker and in scraper_kv.search_key() */
export const queryKey = (q: string) => (q || "").trim().toLowerCase().replace(/\s+/g, " ");

// ------------------------------------------------------------------ scoring
export interface Groups { tags?: string[]; categories?: string[]; models?: string[]; studios?: string[]; via?: string[] }
export interface Searchable { title?: string | null; link?: string | null; description?: string | null; groups?: Groups }

const GROUP_WEIGHT: Record<keyof Groups, number> = { categories: 1, models: 1, tags: 0, studios: -1, via: -1 };
const FIELD_ORDER: (keyof Groups)[] = ["categories", "tags", "models", "studios", "via"];

interface Prep {
  title: string[]; titleN: string; slug: string[]; desc: string[];
  fields: { kind: keyof Groups; n: string; w: string[] }[];
  h: string;                                           // everything, normalised: cheap pre-filter
}
const prepCache = new WeakMap<object, Prep>();

function slugWords(link?: string | null): string[] {
  if (!link) return [];
  try { return stems(decodeURIComponent(new URL(link).pathname).replace(/\.(html?|php|aspx?)$/i, "")); } catch { return []; }
}

function prep(o: Searchable): Prep {
  const hit = prepCache.get(o);
  if (hit) return hit;
  const fields: Prep["fields"] = [];
  for (const kind of FIELD_ORDER) for (const v of o.groups?.[kind] || []) { const n = norm(v); if (n) fields.push({ kind, n, w: n.split(" ").map(stem) }); }
  const title = stems(o.title || "");
  const slug = slugWords(o.link);
  const desc = stems((o.description || "").slice(0, 600));
  const p: Prep = {
    title, titleN: norm(o.title), slug, desc, fields,
    h: [norm(o.title), ...fields.map((f) => f.n), norm((o.description || "").slice(0, 600)), slug.join(" ")].join(" | "),
  };
  prepCache.set(o, p);
  return p;
}

const wordScore = (words: string[], st: string, exact: number, prefix: number): number => {
  let best = 0;
  for (const w of words) { if (w === st) return exact; if (best < prefix && w.length > st.length && st.length >= 3 && w.startsWith(st)) best = prefix; }
  return best;
};

export interface Scored { score: number; coverage: number; matches: string[] }

export function scoreSearchable(o: Searchable, q: Query): Scored {
  if (!q.stems.length) return { score: 0, coverage: 0, matches: [] };
  const p = prep(o);
  let total = 0, matched = 0;
  const matches = new Set<string>();
  for (const st of q.stems) {
    let best = 0, label = "";
    const t = wordScore(p.title, st, 12, 9);
    if (t > best) { best = t; label = "title"; }
    for (const f of p.fields) {
      // a field that IS the keyword ("anal" tag for "anal") beats a field that merely contains it
      const base = f.w.length === 1 && f.w[0] === st ? 11 : wordScore(f.w, st, 8, 6);
      if (!base) continue;
      const s = base + GROUP_WEIGHT[f.kind];
      if (s > best) { best = s; label = f.kind; }
    }
    const d = wordScore(p.desc, st, 3, 2);
    if (d > best) { best = d; label = "description"; }
    const sl = wordScore(p.slug, st, 3, 2);
    if (sl > best) { best = sl; label = "url"; }
    if (best > 0) { matched++; total += best; matches.add(label); }
  }
  const coverage = matched / q.stems.length;
  if (q.stems.length > 1) {
    if (p.titleN.includes(q.norm)) { total += 8; matches.add("title"); }
    for (const f of p.fields) if (f.n === q.norm) { total += 10; matches.add(f.kind); break; }
  }
  return { score: total, coverage, matches: [...matches] };
}

/** the structured fields an item may carry (scraper items, index records, Flask items) */
export function groupsOf(it: any): Groups {
  const arr = (v: any): string[] => (Array.isArray(v) ? v.map((x) => (typeof x === "string" ? x : x?.name)).filter(Boolean) : typeof v === "string" ? v.split(/[,;|]/).map((x) => x.trim()).filter(Boolean) : []);
  return { tags: arr(it.tags), categories: arr(it.categories || it.genres), models: arr(it.models || it.stars || it.pornstars), studios: arr(it.studios || it.series), via: arr(it._via) };
}
const asSearchable = (it: any): Searchable => {
  if (it && it.__s) return it.__s;
  const s: Searchable = { title: it.title, link: it.link, description: it.description, groups: groupsOf(it) };
  try { Object.defineProperty(it, "__s", { value: s, enumerable: false }); } catch { /* frozen */ }
  return s;
};

const quality = (it: any) => (it.thumbnail ? 2 : 0) + (it.duration ? 1 : 0) + (it.views ? 1 : 0) + (it.rating ? 1 : 0);

/** Score one item. `trusted` = the site's own search returned it, so it is relevant even if the title shows no keyword. */
export function relevance(it: any, q: Query, trusted = false): { score: number; matches: string[] } | null {
  const r = scoreSearchable(asSearchable(it), q);
  const need = q.stems.length > 1 ? 0.5 : 1;
  if (!trusted && (r.coverage < need || r.score <= 0)) return null;
  let score = r.score * (r.coverage < 1 ? r.coverage * r.coverage : 1) + quality(it);
  if (trusted) score += 8;
  return { score, matches: r.matches };
}

// ------------------------------------------------------------------ the local search index
export interface IndexRecord {
  l: string; t: string; i?: string | null; du?: string; vw?: string; rt?: string; ad?: string; ql?: string; p?: string;
  tg?: string[]; ct?: string[]; md?: string[]; st?: string[]; vi?: string[]; ds?: string; ts?: number;
}
export interface TaxEntry { n: string; u: string; k: "category" | "tag" | "model" | "studio"; h: string }

const memo = new Map<string, { t: number; v: any }>();
/** KV JSON read memoised per isolate: big keys (index, channels bundle, metadata shards) are parsed once, not per request */
export async function memoKV<T = any>(kv: KVNamespace, key: string, ttlMs = 5 * 60_000): Promise<T | null> {
  const hit = memo.get(key);
  if (hit && Date.now() - hit.t < ttlMs) return hit.v as T | null;
  let v: any = null;
  try { v = await kv.get(key, { type: "json", cacheTtl: 300 }); } catch { v = hit ? hit.v : null; }   // KV hiccup: keep serving the old copy
  memo.set(key, { t: Date.now(), v });
  if (memo.size > 40) memo.delete(memo.keys().next().value as string);
  return v as T | null;
}

export async function loadIndex(kv: KVNamespace): Promise<IndexRecord[]> {
  const d = await memoKV<{ records?: IndexRecord[] }>(kv, "search-index");
  return Array.isArray(d?.records) ? d!.records! : [];
}
export async function loadTaxonomy(kv: KVNamespace): Promise<TaxEntry[]> {
  const d = await memoKV<{ entries?: TaxEntry[] }>(kv, "taxonomy-index");
  return Array.isArray(d?.entries) ? d!.entries! : [];
}

const recSearchable = new WeakMap<IndexRecord, Searchable>();
function recView(r: IndexRecord): Searchable {
  let s = recSearchable.get(r);
  if (!s) { s = { title: r.t, link: r.l, description: r.ds, groups: { tags: r.tg, categories: r.ct, models: r.md, studios: r.st, via: r.vi } }; recSearchable.set(r, s); }
  return s;
}

/** Search every video the scraper knows. No network: a few thousand string checks. */
export function searchIndex(records: IndexRecord[], q: Query, limit = 150): any[] {
  if (!q.stems.length || !records.length) return [];
  const need = Math.max(1, Math.ceil(q.stems.length * 0.5));
  const out: { it: any; score: number }[] = [];
  for (const r of records) {
    const s = recView(r);
    const p = prep(s);
    let quick = 0;
    for (const st of q.stems) if (p.h.includes(st)) quick++;
    if (quick < need) continue;
    const sc = scoreSearchable(s, q);
    const needCov = q.stems.length > 1 ? 0.5 : 1;
    if (sc.coverage < needCov || sc.score <= 0) continue;
    const it = {
      title: r.t, thumbnail: r.i || null, video_src: null, link: r.l, page: r.p || r.l, uid: hash12(r.l),
      ...(r.du ? { duration: r.du } : {}), ...(r.vw ? { views: r.vw } : {}), ...(r.rt ? { rating: r.rt } : {}),
      ...(r.ad ? { added: r.ad } : {}), ...(r.ql ? { quality: r.ql } : {}),
      tags: r.tg || [], categories: r.ct || [], models: r.md || [], studios: r.st || [],
      _via: "index", _match: sc.matches,
    };
    out.push({ it, score: sc.score * (sc.coverage < 1 ? sc.coverage * sc.coverage : 1) + quality(it) });
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, limit).map((x) => x.it);
}

// ------------------------------------------------------------------ taxonomy (category / tag / star / studio pages)
export const regHost = (host: string): string => {
  const p = host.toLowerCase().replace(/^www\d*\./, "").split(".");
  return p.length <= 2 ? p.join(".") : /^(co|com|org|net|gov|ac)$/.test(p[p.length - 2]) && p[p.length - 1].length === 2 ? p.slice(-3).join(".") : p.slice(-2).join(".");
};
export const hostOfUrl = (u: string): string => { try { return regHost(new URL(u.includes("://") ? u : "https://" + u).hostname); } catch { return ""; } };

export interface TaxMatch { entry: TaxEntry; score: number }
/** taxonomy pages of ONE site whose name equals / contains / is contained in the keyword */
export function matchTaxonomy(entries: TaxEntry[], host: string, q: Query, max = 2): TaxMatch[] {
  if (!q.stems.length) return [];
  const out: TaxMatch[] = [];
  const seen = new Set<string>();
  for (const e of entries) {
    if (e.h !== host || seen.has(e.u)) continue;
    const ns = stems(e.n);
    if (!ns.length) continue;
    let score = 0;
    if (ns.join(" ") === q.stems.join(" ")) score = 3;
    else if (q.stems.every((s) => ns.includes(s))) score = 2;                 // keyword "anal" -> category "Anal Sex"
    else if (ns.every((s) => q.stems.includes(s)) && ns.join("").length >= 4) score = 1;   // "milf anal" -> category "Milf"
    if (score) { seen.add(e.u); out.push({ entry: e, score: score + (e.k === "category" || e.k === "tag" ? 0.2 : 0) }); }
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, max);
}

/** URL shapes a site uses for its taxonomy pages, learnt from the index: [{kind, prefix, slash}] most common first */
export function learnShapes(entries: TaxEntry[], host: string): { kind: string; prefix: string; slash: boolean; origin: string }[] {
  const count = new Map<string, { kind: string; prefix: string; slash: boolean; origin: string; n: number }>();
  for (const e of entries) {
    if (e.h !== host) continue;
    let u: URL; try { u = new URL(e.u); } catch { continue; }
    const parts = u.pathname.split("/").filter(Boolean);
    if (!parts.length || u.search) continue;
    const prefix = "/" + parts.slice(0, -1).join("/") + (parts.length > 1 ? "/" : "");
    const slash = u.pathname.endsWith("/");
    const key = `${e.k}|${prefix}|${slash}`;
    const c = count.get(key) || { kind: e.k, prefix, slash, origin: u.origin, n: 0 };
    c.n++; count.set(key, c);
  }
  const best = new Map<string, { kind: string; prefix: string; slash: boolean; origin: string; n: number }>();
  for (const c of count.values()) { const b = best.get(c.kind); if (!b || c.n > b.n) best.set(c.kind, c); }
  return [...best.values()].filter((c) => c.n >= 3).sort((a, b) => b.n - a.n).map(({ kind, prefix, slash, origin }) => ({ kind, prefix, slash, origin }));
}

// ------------------------------------------------------------------ one ranked list
/** Merge every per-site result (+ index hits) into one list, de-duplicated by link and ranked by relevance. */
export function rankCombined(results: any[], q: Query, indexHits: any[] = [], cap = 300): any[] {
  const seen = new Map<string, any>();
  const put = (it: any, site: string, trusted: boolean, via: string) => {
    const k = noQuery(it.link || "");
    if (!k) return;
    const r = relevance(it, q, trusted);
    if (!r) return;
    const prev = seen.get(k);
    if (prev && prev._score >= r.score) { if (!prev._matches.includes(via)) prev._matches.push(via); return; }
    const o = { ...it, _site: site, _score: Math.round(r.score * 10) / 10, _match: r.matches, _matches: [via] };
    if (prev) for (const g of ["tags", "categories", "models", "studios"]) if (!o[g]?.length && prev[g]?.length) o[g] = prev[g];
    seen.set(k, o);
  };
  for (const res of results) {
    const site = res.site || res.page || "";
    const via = res.source === "taxonomy" ? `taxonomy:${res.via || ""}` : "site";
    for (const it of res.items || []) put(it, site, true, via);
  }
  for (const it of indexHits) put(it, it.page || "index", false, "index");
  const list = [...seen.values()].map(({ _matches, ...rest }) => ({ ...rest, _via: _matches.join(",") }));
  list.sort((a, b) => b._score - a._score);
  return list.slice(0, cap);
}
