import { Env } from "./types";
import { scrapePage, searchOne, combineResults, isBlockedHost } from "./scrape";
import { scrapeListing } from "./listings";
import { resolveVideo, resolveFull } from "./resolve";
import { fetchMetadata } from "./metadata";
import { fetchLiveCams } from "./cams";
import { fetchChannelsNative } from "./channels";

// ---------------------------------------------------------------------------------------------------------
// API Worker - built so the site keeps working even when GitHub, a cam platform or your PC is down.
//
// Answer order for every endpoint:   live backend (BACKEND_URL)  ->  Worker scrapes the site itself
//                                    ->  KV (scraper data, ANY age)  ->  last-known-good copy at the edge  ->  error
//
// Reliability features
//   * stale-while-revalidate: old KV data is served instantly while a refresh runs in the background
//   * last-known-good (LKG): every good answer is also kept ~3 days at the edge for when all sources fail
//   * cams refresh themselves: Cloudflare cron trigger + background refresh (no GitHub needed)
//   * watchdog: the cron starts the GitHub scraper workflow when channels/cams/wanted-queue are stale
//   * negative caching (45 s) so a blocked site isn't hammered, in-flight de-duplication, per-IP rate limit
//   * SSRF protection: user-supplied URLs must belong to known sources (see ALLOWED_HOSTS)
//   * misses are queued (`wanted:queue`) so the next scraper run fetches exactly what visitors asked for
//
// KV keys (must match scraper_kv.py):
//   scrape:{url}   search:{site}:{query}   categories:{url}:{mode}:{page}   livecams:{url|default}
//   channels-bundle:{category|all} -> {pages:{"1":{..}}}   resolve:{url} / resolve-full:{url}
//   meta-shard:{sha1(url)[0]} -> {items:{url:meta}}   cam-thumb-origins   scrape-status (CACHE ns)
// ---------------------------------------------------------------------------------------------------------

const EDGE_TTL = 300;
const LIVE_EDGE_TTL = 60;                 // KV edge-cache for fast-changing keys (cams / channels)
const NOT_CACHED = "Not in cache - run scraper";
const WANTED_KEY = "wanted:queue";
const WANTED_MAX = 200;
const WANTED_PER_DAY = 120;
const CAMS_KEY = "livecams:default";
const CAMS_STALE = 20 * 60;               // refresh cams in the background when older than this
const CHANNELS_STALE = 2 * 60 * 60;       // ask GitHub for a channel refresh when older than this
const KV_LONG = 30 * 86400;               // Worker-written KV entries live 30 days
const LKG_TTL = 3 * 86400;
const NEG_TTL = 45;

const LIVE_TTL: Record<string, number> = {
  "/api/scrape": 600, "/api/search": 300, "/api/resolve": 300, "/api/resolve-full": 300,
  "/api/metadata": 3600, "/api/livecams": 60, "/api/channels": 120, "/api/scrape-categories": 900,
};
const LIVE_TIMEOUT: Record<string, number> = {
  "/api/scrape": 28000, "/api/search": 28000, "/api/resolve": 20000, "/api/resolve-full": 25000,
  "/api/metadata": 15000, "/api/livecams": 20000, "/api/channels": 20000, "/api/scrape-categories": 25000,
};
const BUILTIN_HOSTS = ["superporn.com", "pornvideobb.com", "freesexvideos.xxx", "bdsmhole.com", "lemoncams.com", "fullporno.to", "porno-666.me", "xlivetv.com"];
// image CDNs that need the platform's own Referer (used when the scraper's origin map is missing/stale)
const THUMB_REFERERS: [RegExp, string][] = [
  [/doppiocdn|strpst|stripcdn|stripchat/i, "https://stripchat.com/"],
  [/mmcdn|highwebmedia|chaturbate/i, "https://chaturbate.com/"],
  [/xcdnpro|cam4/i, "https://www.cam4.com/"],
  [/camsoda/i, "https://www.camsoda.com/"],
  [/lemoncams/i, "https://www.lemoncams.com/"],
  [/xlivetv/i, "https://xlivetv.com/"],
];

// ------------------------------------------------------------------------------------------------ helpers
function corsHeaders(origin: string | null): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Expose-Headers": "X-Source, X-Cache-Age",
    "Access-Control-Max-Age": "600",
    "Vary": "Origin",
  };
}
async function kvGet<T = any>(kv: KVNamespace, key: string, cacheTtl = EDGE_TTL): Promise<T | null> {
  try { return (await kv.get(key, { type: "json", cacheTtl })) as T | null; } catch { return null; }   // a KV hiccup must not be a 500
}
function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...headers } });
}
const err = (message: string, status = 400, headers: Record<string, string> = {}) => json({ error: message }, status, headers);
async function sha1Hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
const nowSec = () => Math.floor(Date.now() / 1000);
const ageOf = (d: any): number | null => (d && typeof d._ts === "number" ? Math.max(0, nowSec() - d._ts) : null);

function isGoodPayload(d: any): boolean {
  if (!d || typeof d !== "object") return false;
  const has = !!(d.items?.length || d.combined?.length || d.categories?.length || d.sections?.length || d.video || d.groups?.length);
  if (d.error && !has) return false;
  if (Array.isArray(d.results) && d.results.length) {
    const anyData = d.results.some((r: any) => r && ((r.items || []).length || (r.categories || []).length));
    if (!anyData && !has) return false;
  }
  return true;
}

// registrable domain (good enough for the hosts we deal with)
const SLD = new Set(["co", "com", "org", "net", "gov", "ac"]);
function registrable(host: string): string {
  const p = host.toLowerCase().replace(/\.$/, "").split(".");
  if (p.length <= 2) return p.join(".");
  const tail = p.slice(-2);
  return SLD.has(tail[0]) && tail[1].length === 2 ? p.slice(-3).join(".") : tail.join(".");
}
let allowMemo: { t: number; set: Set<string> } | null = null;
async function getAllowed(env: Env): Promise<Set<string>> {
  if (allowMemo && Date.now() - allowMemo.t < 5 * 60_000) return allowMemo.set;
  const set = new Set<string>(BUILTIN_HOSTS);
  for (const h of (env.ALLOWED_HOSTS || "").split(",")) if (h.trim()) set.add(registrable(h.trim().replace(/^https?:\/\//, "").split("/")[0]));
  const grab = (v: any) => {
    if (typeof v === "string" && /^https?:\/\//i.test(v)) { try { set.add(registrable(new URL(v).hostname)); } catch { /* skip */ } }
    else if (Array.isArray(v)) v.forEach(grab);
    else if (v && typeof v === "object") Object.values(v).forEach(grab);
  };
  grab(await kvGet(env.SCRAPE_DATA, "catalog:urls"));
  grab(await kvGet(env.SCRAPE_DATA, "test-sites"));
  allowMemo = { t: Date.now(), set };
  return set;
}
async function urlAllowed(env: Env, u: string): Promise<boolean> {
  try {
    const p = new URL(u.includes("://") ? u : "https://" + u);
    if (!/^https?:$/.test(p.protocol) || isBlockedHost(p.hostname)) return false;
    if (env.ALLOW_ANY === "1") return true;
    return (await getAllowed(env)).has(registrable(p.hostname));
  } catch { return false; }
}

// in-isolate de-duplication of identical in-flight work
const inflight = new Map<string, Promise<any>>();
function once<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const hit = inflight.get(key);
  if (hit) return hit;
  const p = fn().finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

// ------------------------------------------------------------------------------- cams: the self-refresher
/** Fetch cams from the platforms and store them in KV (30-day TTL). Safe to call from many places at once. */
async function refreshCams(env: Env, force = false): Promise<any | null> {
  return once("refresh-cams", async () => {
    let cur: any = null;
    try { cur = await env.SCRAPE_DATA.get(CAMS_KEY, { type: "json" }); } catch { /* treat as empty */ }
    const age = ageOf(cur);
    if (!force && cur?.items?.length && age !== null && age < 300) return cur;          // refreshed a moment ago
    const fresh = await fetchLiveCams().catch(() => null);
    if (!fresh || !fresh.items?.length) return null;
    // a platform that failed this time keeps its previous (recent) cams so the page never shrinks to one platform
    if (cur?.items?.length && age !== null && age < 3 * 3600) {
      const got = new Set(fresh.items.map((c: any) => c.provider));
      const extra = cur.items.filter((c: any) => c.provider && !got.has(c.provider));
      if (extra.length) { fresh.items = fresh.items.concat(extra).slice(0, 500); fresh.count = fresh.items.length; fresh.carried_over = [...new Set(extra.map((c: any) => c.provider_name || c.provider))]; }
    }
    try { await env.SCRAPE_DATA.put(CAMS_KEY, JSON.stringify(fresh), { expirationTtl: KV_LONG }); } catch { /* serve it anyway */ }
    return fresh;
  });
}

/** Start the GitHub scraper workflow (needs GITHUB_TOKEN + GITHUB_REPO). Locked so it can't be spammed. */
async function dispatchGithub(env: Env, why: string): Promise<boolean> {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return false;
  try {
    if (await env.CACHE.get("lock:dispatch")) return false;
    await env.CACHE.put("lock:dispatch", why, { expirationTtl: 20 * 60 });
    const wf = env.GITHUB_WORKFLOW || "scrape-live.yml";
    const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${wf}/dispatches`, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "archive-worker", "Content-Type": "application/json" },
      body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs: { only: "live" } }),
    });
    return r.status === 204;
  } catch { return false; }
}

async function freshness(env: Env) {
  const cams = await kvGet<any>(env.SCRAPE_DATA, CAMS_KEY, 60);
  const ch = await kvGet<any>(env.SCRAPE_DATA, "channels-bundle:all", 60);
  const status = await kvGet<any>(env.CACHE, "scrape-status", 60);
  const wanted = await kvGet<any>(env.CACHE, WANTED_KEY, 60);
  return {
    livecams: { items: cams?.items?.length || 0, age_s: ageOf(cams) },
    channels: { pages: Object.keys(ch?.pages || {}).length, age_s: ageOf(ch) },
    wanted_queue: wanted?.items?.length || 0,
    last_scrape: status || null,
  };
}

/** Cron: keep cams fresh on our own, and start GitHub when it is behind. */
async function maintenance(env: Env): Promise<void> {
  const cams = await kvGet<any>(env.SCRAPE_DATA, CAMS_KEY, 60);
  const camAge = ageOf(cams);
  if (!cams?.items?.length || camAge === null || camAge > CAMS_STALE) await refreshCams(env);
  const f = await freshness(env);
  const camsStillStale = !f.livecams.items || f.livecams.age_s === null || f.livecams.age_s > 2 * CAMS_STALE;
  const chStale = !f.channels.pages || f.channels.age_s === null || f.channels.age_s > CHANNELS_STALE;
  if (chStale || camsStillStale) await dispatchGithub(env, chStale ? "channels-stale" : "cams-stale");
  else if (f.wanted_queue > 0) await dispatchGithub(env, "wanted-queue");
}

// ----------------------------------------------------------------------------------------------- the Worker
export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(maintenance(env).catch(() => undefined));
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const headers = corsHeaders(request.headers.get("Origin"));
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });

    const isPost = request.method === "POST";
    const isGet = request.method === "GET";
    const bodyText = isPost ? await request.text() : "";
    const body = async <T>(): Promise<T> => { try { return JSON.parse(bodyText || "{}") as T; } catch { return {} as T; } };

    const backend = (env.BACKEND_URL || "").replace(/\/$/, "");
    const liveFirst = !!backend && (env.MODE || "live").toLowerCase() !== "kv";
    const nativeOn = (env.LIVE_SCRAPE || "on").toLowerCase() !== "off";
    const nativeFirst = nativeOn && (env.MODE || "live").toLowerCase() !== "kv";
    const ttl = LIVE_TTL[url.pathname] ?? 120;
    const forced = (() => { try { const j = JSON.parse(bodyText || "{}"); return !!(j.force || j.fresh); } catch { return false; } })();
    const routeKey = `${url.pathname}/${await sha1Hex(bodyText + "|" + url.search)}`;

    const edgeCache = (caches as any).default as Cache;
    const ek = (k: string) => new Request(`https://edge-cache.invalid/${k}`);
    const edgeGetJson = async (k: string): Promise<any | null> => { try { const h = await edgeCache.match(ek(k)); return h ? await h.json() : null; } catch { return null; } };
    const edgePutJson = (k: string, data: unknown, seconds: number) =>
      ctx.waitUntil(edgeCache.put(ek(k), new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json", "Cache-Control": `public, max-age=${seconds}` } })).catch(() => undefined));
    /** short-lived answer + a long-lived last-known-good copy */
    const remember = (k: string, data: unknown, seconds: number) => { edgePutJson(k, data, seconds); edgePutJson("lkg/" + k, data, LKG_TTL); };

    const withCors = (r: Response): Response => {
      const o = new Response(r.body, r);
      for (const [k, v] of Object.entries(headers)) o.headers.set(k, v);
      o.headers.delete("Cache-Control");
      return o;
    };
    const send = (data: unknown, source: string, age?: number | null) => {
      const o = json(data, 200, headers);
      o.headers.set("X-Source", source);
      if (age !== null && age !== undefined) o.headers.set("X-Cache-Age", String(age));
      return o;
    };

    // ---- live backend (Flask) -------------------------------------------------------------------
    const viaBackend = async (): Promise<Response | null> => {
      if (!backend) return null;
      const key = ek(`backend/${routeKey}`);
      if (!forced) { const hit = await edgeCache.match(key); if (hit) { const o = withCors(hit); o.headers.set("X-Source", "edge"); return o; } }
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), LIVE_TIMEOUT[url.pathname] ?? 20000);
      try {
        const r = await fetch(backend + url.pathname + url.search, { method: request.method, headers: { "Content-Type": "application/json" }, body: isPost ? bodyText : undefined, signal: ac.signal });
        if (!r.ok) return null;
        const text = await r.text();
        let parsed: any; try { parsed = JSON.parse(text); } catch { return null; }
        if (!isGoodPayload(parsed)) return null;
        ctx.waitUntil(edgeCache.put(key, new Response(text, { headers: { "Content-Type": "application/json", "Cache-Control": `public, max-age=${ttl}` } })).catch(() => undefined));
        edgePutJson("lkg/" + routeKey, parsed, LKG_TTL);
        const out = withCors(new Response(text, { headers: { "Content-Type": "application/json" } }));
        out.headers.set("X-Source", "live");
        return out;
      } catch { return null; } finally { clearTimeout(timer); }
    };
    const tryLive = async () => (liveFirst ? viaBackend() : null);
    const missLive = async () => (liveFirst ? null : viaBackend());

    // ---- misses are queued for the scraper ------------------------------------------------------
    const want = (item: { t: string; [k: string]: any }) => {
      ctx.waitUntil((async () => {
        try {
          const q = (await env.CACHE.get(WANTED_KEY, { type: "json" })) as { day?: string; n?: number; items?: any[] } | null;
          const today = new Date().toISOString().slice(0, 10);
          const cur = { day: today, n: q && q.day === today ? q.n || 0 : 0, items: q?.items || [] };
          const id = JSON.stringify(item);
          if (cur.items.some((x) => JSON.stringify(x) === id) || cur.n >= WANTED_PER_DAY) return;
          cur.items.push(item);
          if (cur.items.length > WANTED_MAX) cur.items.splice(0, cur.items.length - WANTED_MAX);
          cur.n += 1;
          await env.CACHE.put(WANTED_KEY, JSON.stringify(cur), { expirationTtl: 3 * 86400 });
        } catch { /* best effort */ }
      })());
    };

    // ---- native scraping (the Worker fetches the sites itself) ------------------------------------
    const rateLimited = async (): Promise<boolean> => {
      const limit = Math.max(10, Number(env.RATE_PER_MIN) || 90);
      const ip = request.headers.get("CF-Connecting-IP") || "anon";
      const k = ek(`rl/${ip}/${Math.floor(Date.now() / 60000)}`);
      const hit = await edgeCache.match(k).catch(() => undefined);
      const n = hit ? Number(await hit.text()) || 0 : 0;
      if (n >= limit) return true;
      ctx.waitUntil(edgeCache.put(k, new Response(String(n + 1), { headers: { "Cache-Control": "public, max-age=70" } })).catch(() => undefined));
      return false;
    };
    /** edge cache -> (negative cache / rate limit) -> scrape once -> keep result; on failure fall back to the last-known-good copy */
    const native = async (k: string, seconds: number, run: () => Promise<any>, good: (d: any) => boolean): Promise<any | null> => {
      if (!forced) { const c = await edgeGetJson(k); if (c) return c; }
      if ((await edgeGetJson("neg/" + k)) || (await rateLimited())) return edgeGetJson("lkg/" + k);
      const d = await once(k, () => run().catch(() => null));
      if (d && !d.error && good(d)) { remember(k, d, seconds); return d; }
      edgePutJson("neg/" + k, { f: 1 }, NEG_TTL);
      return edgeGetJson("lkg/" + k);
    };
    const nativePage = async (u: string, pageNum: number | null) =>
      (await urlAllowed(env, u)) ? native(`page/${await sha1Hex(u + "|" + (pageNum ?? ""))}`, 600, () => scrapePage(u, 80, pageNum), (d) => !!d.items?.length) : null;
    const nativeSearchSite = async (site: string, query: string) => {
      if (!(await urlAllowed(env, site))) return null;
      const shapeKey = `shape/${await sha1Hex(site)}`;
      return native(`search/${await sha1Hex(site + "|" + query)}`, 300, async () => {
        const pref = await edgeGetJson(shapeKey);
        const { result, winner } = await searchOne(site, query, 40, typeof pref?.i === "number" ? pref.i : null);
        if (winner !== null) edgePutJson(shapeKey, { i: winner }, 7 * 86400);
        return result;
      }, (d) => !!d.items?.length);
    };
    const nativeSearch = async (sites: string[], query: string): Promise<Response | null> => {
      const rs = (await Promise.all(sites.slice(0, 8).map((x) => nativeSearchSite(x, query).catch(() => null)))).filter(Boolean) as any[];
      if (!rs.length) return null;
      const combined = combineResults(rs, query);
      return combined.length ? send({ results: rs, query, combined, count: combined.length }, "native") : null;
    };
    const nativeGuarded = async (u: string, k: string, seconds: number, run: () => Promise<any>, good: (d: any) => boolean) =>
      (await urlAllowed(env, u)) ? native(k, seconds, run, good) : null;

    /** last resort for single-payload routes */
    const lkgOr = async (fallback: unknown) => {
      const stale = await edgeGetJson("lkg/" + routeKey);
      return stale ? send(stale, "stale-edge") : json(fallback, 200, headers);
    };

    try {
      switch (url.pathname) {
        case "/healthz":
          if (isGet) return json({ ok: true, live_backend: !!backend, mode: liveFirst ? "live-first" : "kv-first", native: nativeOn, ...(await freshness(env)) }, 200, headers);
          break;
        case "/api/status":
          if (isGet) return json(await freshness(env), 200, headers);
          break;

        case "/api/catalog-urls":
          if (isGet) {
            const d = await kvGet(env.SCRAPE_DATA, "catalog:urls");
            if (d) edgePutJson("lkg/catalog", d, LKG_TTL);
            return json(d || (await edgeGetJson("lkg/catalog")) || {}, 200, headers);
          }
          break;
        case "/api/test-sites":
          if (isGet) {
            const d = await kvGet(env.SCRAPE_DATA, "test-sites");
            if (d) edgePutJson("lkg/test-sites", d, LKG_TTL);
            return json(d || (await edgeGetJson("lkg/test-sites")) || { sites: [] }, 200, headers);
          }
          break;

        case "/api/scrape": {
          if (!isPost) break;
          const b = await body<{ urls?: any[]; url?: string }>();
          const raw = b.urls || (b.url ? [b.url] : []);
          const reqs = raw.map((u: any) => (typeof u === "string" ? { url: u, page_num: null } : { url: u?.url, page_num: u?.page_num ?? null })).filter((u: any) => u.url).slice(0, 12);
          if (!reqs.length) return err("No urls provided", 400, headers);

          const live = await tryLive();
          if (live) return live;

          const fresh: (any | null)[] = nativeFirst ? await Promise.all(reqs.map((r) => nativePage(r.url, r.page_num).catch(() => null))) : reqs.map(() => null);
          const hits = await Promise.all(reqs.map((r, i) => (fresh[i] ? fresh[i] : kvGet(env.SCRAPE_DATA, `scrape:${r.url}`))));
          if (!nativeFirst && nativeOn && hits.some((h) => !h || (h as any).error)) {
            const again = await Promise.all(reqs.map((r, i) => (hits[i] && !(hits[i] as any).error ? hits[i] : nativePage(r.url, r.page_num).catch(() => null))));
            again.forEach((a, i) => { if (a) hits[i] = a; });
          }
          if (hits.some((h) => !h || (h as any).error)) { const l = await missLive(); if (l) return l; }
          const results = hits.map((h: any, i) => {
            if (h && !h.error) return { ...h, page_num: reqs[i].page_num ?? h.page_num };
            if (!h) want({ t: "scrape", url: reqs[i].url, page_num: reqs[i].page_num });
            const first = (reqs[i].page_num ?? 1) <= 1;
            return h || { page: reqs[i].url, items: [], count: 0, next_page: null, page_num: reqs[i].page_num ?? 1, not_cached: true, ...(first ? { error: NOT_CACHED } : {}) };
          });
          return json({ results }, 200, headers);
        }

        case "/api/search": {
          if (!isPost) break;
          const b = await body<{ sites?: string[]; query?: string }>();
          const sites = (b.sites || []).filter(Boolean);
          const query = (b.query || "").trim().toLowerCase();
          if (!sites.length || !query) return err("sites and query are required", 400, headers);
          const live = await tryLive();
          if (live) return live;
          if (nativeFirst) { const n = await nativeSearch(sites, query); if (n) return n; }
          const all = await Promise.all(sites.map((s) => kvGet<any>(env.SCRAPE_DATA, `search:${s}:${query}`)));
          sites.forEach((s, i) => { if (!all[i] || all[i].error) want({ t: "search", site: s, query }); });
          const parts = all.filter((p) => p && !p.error) as any[];
          if (!parts.length) {
            const l = await missLive(); if (l) return l;
            if (nativeOn && !nativeFirst) { const n = await nativeSearch(sites, query); if (n) return n; }
            return lkgOr({ results: [], query, combined: [], count: 0, error: NOT_CACHED });
          }
          const combined = parts.flatMap((p) => p.combined || []).sort((a, b) => (b._score || 0) - (a._score || 0));
          const out = { results: parts.flatMap((p) => p.results || []), query, combined, count: combined.length };
          edgePutJson("lkg/" + routeKey, out, LKG_TTL);
          return send(out, "kv");
        }

        case "/api/resolve":
        case "/api/resolve-full": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          if (!b.url) return err("url is required", 400, headers);
          const full = url.pathname === "/api/resolve-full";
          const live = await tryLive();
          if (live) return live;
          const prefix = full ? "resolve-full" : "resolve";
          const hasVideo = (d: any) => !!d?.video;
          const runNative = async () => nativeGuarded(b.url!, `${prefix}|${await sha1Hex(b.url!)}`, 300, async () => {
            if (full) return resolveFull(b.url!);
            const r = await resolveVideo(b.url!, true);
            return !r.video && !r.error ? resolveVideo(b.url!, false) : r;
          }, hasVideo);
          if (nativeFirst) { const n = await runNative(); if (n) return send(n, "native"); }
          const data = await kvGet(env.SCRAPE_DATA, `${prefix}:${b.url}`);
          if (data && hasVideo(data)) return send(data, "kv", ageOf(data));
          const l = await missLive(); if (l) return l;
          if (nativeOn && !nativeFirst) { const n = await runNative(); if (n) return send(n, "native"); }
          if (!data) want({ t: "resolve", url: b.url, full });
          return data ? json(data, 200, headers) : lkgOr({ video: null, error: NOT_CACHED });
        }

        case "/api/metadata": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          const u = (b.url || "").trim();
          if (!u) return err("valid url is required", 400, headers);
          const shard = (await sha1Hex(u))[0];
          const bundle = await kvGet<{ items?: Record<string, any> }>(env.SCRAPE_DATA, `meta-shard:${shard}`);
          const hit = bundle?.items?.[u] ?? (await kvGet<any>(env.SCRAPE_DATA, `meta:${u}`));
          if (hit && !forced) { const { _ts, ...meta } = hit; return send(meta, "kv"); }
          const live = await viaBackend(); if (live) return live;
          if (nativeOn) {
            const n = await nativeGuarded(u, `meta|${await sha1Hex(u)}`, 3600, () => fetchMetadata(u), (d) => !!(d.title || d.duration || (d.groups || []).length));
            if (n) return send(n, "native");
          }
          return lkgOr({ url: u, groups: [], error: NOT_CACHED });
        }

        // ---------------------------------------------------------------- live cams (never an error if ANY data exists)
        case "/api/livecams": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          const key = `livecams:${b.url || "default"}`;
          const isDefault = key === CAMS_KEY;
          const live = await tryLive();
          if (live) return live;

          const data = await kvGet<any>(env.SCRAPE_DATA, key, LIVE_EDGE_TTL);
          const have = !!data?.items?.length;
          const age = ageOf(data);
          if (have) {
            // stale-while-revalidate: answer now, refresh in the background
            if (isDefault && nativeOn && (age === null || age > CAMS_STALE)) ctx.waitUntil(refreshCams(env).then(() => undefined).catch(() => undefined));
            edgePutJson("lkg/" + routeKey, data, LKG_TTL);
            return send(data, "kv", age);
          }
          // KV empty / expired / only an error: the edge copy, then fetch right now
          const stale = await edgeGetJson("lkg/" + routeKey);
          if (stale?.items?.length) {
            if (isDefault && nativeOn) ctx.waitUntil(refreshCams(env).then(() => undefined).catch(() => undefined));
            return send(stale, "stale-edge");
          }
          if (isDefault && nativeOn) {
            const fresh = await refreshCams(env);
            if (fresh?.items?.length) { edgePutJson("lkg/" + routeKey, fresh, LKG_TTL); return send(fresh, "native", 0); }
          }
          const l = await missLive(); if (l) return l;
          ctx.waitUntil(dispatchGithub(env, "cams-missing").then(() => undefined));
          return json(data || { items: [], count: 0, error: NOT_CACHED }, 200, headers);
        }

        // ---------------------------------------------------------------- live TV channels (bundle, stale-forever)
        case "/api/channels": {
          if (!isPost) break;
          const b = await body<{ category?: string | null; page?: number }>();
          const cat = b.category || "all";
          const page = Math.max(1, Number(b.page) || 1);
          const live = await tryLive();
          if (live) return live;

          const bundle = await kvGet<{ pages?: Record<string, any>; _ts?: number }>(env.SCRAPE_DATA, `channels-bundle:${cat}`, LIVE_EDGE_TTL);
          const age = ageOf(bundle);
          if (bundle?.pages && Object.keys(bundle.pages).length) {
            if (cat === "all" && (age === null || age > CHANNELS_STALE)) ctx.waitUntil(dispatchGithub(env, "channels-stale").then(() => undefined));
            const pg = bundle.pages[String(page)];
            if (pg && !pg.error) { edgePutJson(`lkg/channels/${cat}/${page}`, pg, LKG_TTL); return send(pg, "kv", age); }
            // asked past the last scraped page = end of the list (never an error while we hold data)
            return send({ items: [], count: 0, next_page: null, page, end: true }, "kv", age);
          }
          const legacy = await kvGet<any>(env.SCRAPE_DATA, `channels:${cat}:${page}`, LIVE_EDGE_TTL);
          if (legacy && !legacy.error) return send(legacy, "kv");
          const stale = await edgeGetJson(`lkg/channels/${cat}/${page}`);
          if (stale) return send(stale, "stale-edge");
          // nothing from the scraper yet: fetch xlivetv.com right here (Cloudflare's network), then keep it in KV
          if (nativeOn) {
            const nat = await native(`channels/${cat}/${page}`, 600, () => fetchChannelsNative(cat === "all" ? null : cat, page), (d) => !!d.items?.length || !!d.end);
            if (nat) {
              if (nat.items?.length) ctx.waitUntil(env.SCRAPE_DATA.put(`channels:${cat}:${page}`, JSON.stringify({ ...nat, _ts: nowSec() }), { expirationTtl: 12 * 3600 }).catch(() => undefined));
              return send(nat, "native", 0);
            }
          }
          const l = await missLive(); if (l) return l;
          want({ t: "channels", category: cat === "all" ? null : cat, page });
          ctx.waitUntil(dispatchGithub(env, "channels-missing").then(() => undefined));
          if (page > 1) return json({ items: [], count: 0, next_page: null, page, end: true }, 200, headers);
          return json({ items: [], count: 0, error: NOT_CACHED }, 200, headers);
        }

        case "/api/scrape-categories": {
          if (!isPost) break;
          const b = await body<{ url?: string; mode?: string; page_num?: number }>();
          if (!b.url) return err("url is required", 400, headers);
          let mode = b.mode === "pornstars" ? "models" : b.mode || "categories";
          try { const p = new URL(b.url); if (p.hostname.endsWith("freesexvideos.xxx") && p.pathname.toLowerCase().startsWith("/models")) mode = "models"; } catch { /* keep */ }
          const live = await tryLive(); if (live) return live;
          const pn = b.page_num ?? null;
          const good = (d: any) => !!((d.categories || []).length || (d.sections || []).length || (d.models || []).length);
          const runNative = async () => nativeGuarded(b.url!, `cat|${await sha1Hex(`${b.url}|${mode}|${pn ?? 1}`)}`, 900, () => scrapeListing(b.url!, mode, pn), good);
          if (nativeFirst) { const n = await runNative(); if (n) return send(n, "native"); }
          const data = await kvGet(env.SCRAPE_DATA, `categories:${b.url}:${mode}:${b.page_num || 1}`);
          if (data && !(data as any).error) return send(data, "kv", ageOf(data));
          const l = await missLive(); if (l) return l;
          if (nativeOn && !nativeFirst) { const n = await runNative(); if (n) return send(n, "native"); }
          if (!data) want({ t: "categories", url: b.url, mode, page_num: b.page_num || 1 });
          return data ? json(data, 200, headers) : lkgOr({ categories: [], count: 0, error: NOT_CACHED });
        }

        case "/api/cam-thumb": {
          if (!isGet) break;
          const raw = (url.searchParams.get("u") || "").trim().replace(/[?&]_t=\d+$/, "");
          if (!raw) return err("u is required", 400, headers);
          let target: URL;
          try { target = new URL(raw); } catch { return err("bad url", 400, headers); }
          if (target.protocol !== "https:" || isBlockedHost(target.hostname)) return err("blocked url", 400, headers);
          const map = await kvGet<{ origins?: Record<string, string> }>(env.SCRAPE_DATA, "cam-thumb-origins", LIVE_EDGE_TTL);
          const byHost = THUMB_REFERERS.find(([rx]) => rx.test(target.hostname))?.[1];
          const referer = map?.origins?.[raw] || byHost || target.origin + "/";
          const upstream = await fetch(target.toString(), {
            headers: { "User-Agent": "Mozilla/5.0", Accept: "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", Referer: referer, Origin: referer.replace(/\/$/, "") },
            cf: { cacheTtl: 30, cacheEverything: true },
          } as RequestInit).catch(() => null);
          const type = (upstream?.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
          if (!upstream || !upstream.ok || !type.startsWith("image/")) return new Response(null, { status: 404, headers: { ...headers, "Cache-Control": "public, max-age=30" } });
          return new Response(upstream.body, { status: 200, headers: { ...headers, "Content-Type": type, "Cache-Control": "public, max-age=30" } });
        }

        case "/api/translate-titles":
          if (isPost) return json({ titles: [], count: 0 }, 200, headers);
          break;
      }
      return err("Not found", 404, headers);
    } catch (e) {
      // last line of defence: even an unexpected crash answers from the last-known-good copy when there is one
      const stale = await edgeGetJson("lkg/" + routeKey).catch(() => null);
      if (stale) return send(stale, "stale-edge");
      return err(e instanceof Error ? e.message : "Internal error", 500, headers);
    }
  },
};
