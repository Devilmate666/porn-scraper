import { Env } from "./types";
import { scrapePage, searchOne, combineResults } from "./scrape";

// ---------------------------------------------------------------------------
// API Worker. Two data sources, used like the always-running Flask server would:
//
//   1. LIVE  - your Flask app (BACKEND_URL, e.g. through a Cloudflare Tunnel). Asked FIRST, with a timeout.
//              Good answers are kept in the colo's Cache API (free, no KV writes) like Flask's own caches.
//   1b. NATIVE - the Worker scrapes the websites itself (src/scrape.ts, a port of scraper.py's generic scraper),
//              so search + scrolling hit the real sites even with no backend running. Edge-cached like Flask.
//   2. KV    - SCRAPE_DATA, filled by the GitHub Actions scraper. Used when the backend is off/slow/failing,
//              and as the only source when BACKEND_URL is not set.
//
// Whatever KV could not answer is remembered in `wanted:queue` (capped, deduped, max N writes/day) so the
// next scraper run fetches exactly what people scrolled/searched for. Pages past the end of the cache are
// reported as "end of list" instead of an error, so infinite scroll finishes cleanly.
//
// KV key formats MUST match scraper_kv.py:
//   scrape:{url}                      search:{site}:{query-lowercased}
//   categories:{url}:{mode}:{page}    livecams:{url|default}
//   channels-bundle:{category|all} -> { pages: { "1": {...}, "2": {...} } }    (legacy: channels:{category|all}:{page})
//   resolve:{url} / resolve-full:{url}
//   meta-shard:{sha1(url)[0]} -> { items: { url: metadata } }     cam-thumb-origins -> { origins: { img: referer } }
// ---------------------------------------------------------------------------

const EDGE_TTL = 300;
const NOT_CACHED = "Not in cache - run scraper";
const WANTED_KEY = "wanted:queue";
const WANTED_MAX = 200;
const WANTED_PER_DAY = 120;

// seconds a live answer stays in the Cache API (Flask uses 300-600s for the same things)
const LIVE_TTL: Record<string, number> = {
  "/api/scrape": 600, "/api/search": 300, "/api/resolve": 300, "/api/resolve-full": 300,
  "/api/metadata": 3600, "/api/livecams": 60, "/api/channels": 120, "/api/scrape-categories": 900,
};
const LIVE_TIMEOUT: Record<string, number> = {
  "/api/scrape": 28000, "/api/search": 28000, "/api/resolve": 20000, "/api/resolve-full": 25000,
  "/api/metadata": 15000, "/api/livecams": 20000, "/api/channels": 20000, "/api/scrape-categories": 25000,
};

function corsHeaders(origin: string | null): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
    "Vary": "Origin",
  };
}

async function kvGet<T = any>(kv: KVNamespace, key: string): Promise<T | null> {
  return (await kv.get(key, { type: "json", cacheTtl: EDGE_TTL })) as T | null;
}

function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const err = (message: string, status = 400, headers: Record<string, string> = {}) =>
  json({ error: message }, status, headers);

async function sha1Hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function isBlockedHost(host: string): boolean {
  const h = host.toLowerCase();
  return (
    h === "localhost" ||
    h.endsWith(".local") ||
    h.endsWith(".internal") ||
    /^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|0\.)/.test(h) ||
    h.includes(":") // IPv6 literals
  );
}

/** A live payload is only worth serving/caching if it actually carries data. */
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

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const headers = corsHeaders(request.headers.get("Origin"));

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });

    const isPost = request.method === "POST";
    const isGet = request.method === "GET";
    const bodyText = isPost ? await request.text() : "";
    const body = async <T>(): Promise<T> => {
      try { return JSON.parse(bodyText || "{}") as T; } catch { return {} as T; }
    };

    // ---------------------------------------------------------------- live backend
    const backend = (env.BACKEND_URL || "").replace(/\/$/, "");
    const liveFirst = !!backend && (env.MODE || "live").toLowerCase() !== "kv";
    const ttl = LIVE_TTL[url.pathname] ?? 120;
    const forced = (() => { try { const j = JSON.parse(bodyText || "{}"); return !!(j.force || j.fresh); } catch { return false; } })();

    const withCors = (r: Response): Response => {
      const o = new Response(r.body, r);
      for (const [k, v] of Object.entries(headers)) o.headers.set(k, v);
      o.headers.delete("Cache-Control");
      return o;
    };

    /** Ask Flask (edge-cached). Returns null when it is off, slow, errors, or returns a failed scrape. */
    const viaBackend = async (): Promise<Response | null> => {
      if (!backend) return null;
      const cache = (caches as any).default as Cache;
      const key = new Request(`https://edge-cache.invalid${url.pathname}/${await sha1Hex(bodyText + "|" + url.search)}`);
      if (!forced) {
        const hit = await cache.match(key);
        if (hit) { const o = withCors(hit); o.headers.set("X-Source", "edge"); return o; }
      }
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), LIVE_TIMEOUT[url.pathname] ?? 20000);
      try {
        const r = await fetch(backend + url.pathname + url.search, {
          method: request.method,
          headers: { "Content-Type": "application/json" },
          body: isPost ? bodyText : undefined,
          signal: ac.signal,
        });
        if (!r.ok) return null;
        const text = await r.text();
        let parsed: any = null;
        try { parsed = JSON.parse(text); } catch { return null; }
        if (!isGoodPayload(parsed)) return null;                       // let KV answer instead of a failed scrape
        const stored = new Response(text, { headers: { "Content-Type": "application/json", "Cache-Control": `public, max-age=${ttl}` } });
        ctx.waitUntil(cache.put(key, stored));
        const out = withCors(new Response(text, { headers: { "Content-Type": "application/json" } }));
        out.headers.set("X-Source", "live");
        return out;
      } catch { return null; } finally { clearTimeout(timer); }
    };
    /** live-first mode: call at the top of a route. */
    const tryLive = async (): Promise<Response | null> => (liveFirst ? viaBackend() : null);
    /** kv-first mode: call after a KV miss. (In live-first mode the backend was already asked.) */
    const missLive = async (): Promise<Response | null> => (liveFirst ? null : viaBackend());

    /** Remember what KV could not answer so the next scraper run fetches it. */
    const want = (item: { t: string; [k: string]: any }) => {
      ctx.waitUntil((async () => {
        try {
          const q = (await env.CACHE.get(WANTED_KEY, { type: "json" })) as { day?: string; n?: number; items?: any[] } | null;
          const today = new Date().toISOString().slice(0, 10);
          const cur = { day: today, n: q && q.day === today ? q.n || 0 : 0, items: q?.items || [] };
          const id = JSON.stringify(item);
          if (cur.items.some((x) => JSON.stringify(x) === id)) return;
          if (cur.n >= WANTED_PER_DAY) return;
          cur.items.push(item);
          if (cur.items.length > WANTED_MAX) cur.items.splice(0, cur.items.length - WANTED_MAX);
          cur.n += 1;
          await env.CACHE.put(WANTED_KEY, JSON.stringify(cur), { expirationTtl: 3 * 86400 });
        } catch { /* best effort */ }
      })());
    };

    // ---------------------------------------------------------------- native live scraping
    // order: backend (if set) -> Worker scrapes the sites itself -> KV. Set LIVE_SCRAPE=off to disable the native step.
    const nativeOn = (env.LIVE_SCRAPE || "on").toLowerCase() !== "off";
    const nativeFirst = nativeOn && (env.MODE || "live").toLowerCase() !== "kv";
    const edgeCache = (caches as any).default as Cache;
    const ek = (k: string) => new Request(`https://edge-cache.invalid/${k}`);
    const edgeGetJson = async (k: string): Promise<any | null> => {
      const hit = await edgeCache.match(ek(k));
      return hit ? hit.json() : null;
    };
    const edgePutJson = (k: string, data: unknown, seconds: number) =>
      ctx.waitUntil(edgeCache.put(ek(k), new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json", "Cache-Control": `public, max-age=${seconds}` } })));

    const nativePage = async (u: string, pageNum: number | null): Promise<any | null> => {
      const k = `page/${await sha1Hex(u + "|" + (pageNum ?? ""))}`;
      if (!forced) { const c = await edgeGetJson(k); if (c) return c; }
      const r = await scrapePage(u, 80, pageNum);
      if (r.error || !r.items.length) return null;                 // failures are never cached
      edgePutJson(k, r, 600);
      return r;
    };

    const nativeSearchSite = async (site: string, query: string): Promise<any | null> => {
      const k = `search/${await sha1Hex(site + "|" + query)}`;
      if (!forced) { const c = await edgeGetJson(k); if (c) return c; }
      const shapeKey = `shape/${await sha1Hex(site)}`;
      const pref = await edgeGetJson(shapeKey);
      const { result, winner } = await searchOne(site, query, 40, typeof pref?.i === "number" ? pref.i : null);
      if (winner !== null) edgePutJson(shapeKey, { i: winner }, 7 * 86400);
      if (!result.items.length) return null;
      edgePutJson(k, result, 300);
      return result;
    };

    const nativeSearch = async (sites: string[], query: string): Promise<Response | null> => {
      const rs = (await Promise.all(sites.slice(0, 8).map((x) => nativeSearchSite(x, query).catch(() => null)))).filter(Boolean) as any[];
      if (!rs.length) return null;
      const combined = combineResults(rs, query);
      if (!combined.length) return null;
      const out = json({ results: rs, query, combined, count: combined.length }, 200, headers);
      out.headers.set("X-Source", "native");
      return out;
    };

    try {
      switch (url.pathname) {
        case "/healthz":
          if (isGet) return json({ ok: true, live: !!backend, mode: liveFirst ? "live-first" : "kv-first" }, 200, headers);
          break;

        case "/api/catalog-urls":
          if (isGet) return json((await kvGet(env.SCRAPE_DATA, "catalog:urls")) || {}, 200, headers);
          break;

        case "/api/test-sites":
          if (isGet) return json((await kvGet(env.SCRAPE_DATA, "test-sites")) || { sites: [] }, 200, headers);
          break;

        case "/api/scrape": {
          if (!isPost) break;
          const b = await body<{ urls?: any[]; url?: string }>();
          const raw = b.urls || (b.url ? [b.url] : []);
          // the frontend sends plain strings or {url, page_num}
          const reqs = raw
            .map((u: any) => (typeof u === "string" ? { url: u, page_num: null } : { url: u?.url, page_num: u?.page_num ?? null }))
            .filter((u: any) => u.url)
            .slice(0, 12);
          if (!reqs.length) return err("No urls provided", 400, headers);

          const live = await tryLive();
          if (live) return live;

          // live: scrape the real pages now (parallel); anything that fails is answered from KV
          const fresh: (any | null)[] = nativeFirst
            ? await Promise.all(reqs.map((r) => nativePage(r.url, r.page_num).catch(() => null)))
            : reqs.map(() => null);
          const hits = await Promise.all(reqs.map((r, i) => (fresh[i] ? fresh[i] : kvGet(env.SCRAPE_DATA, `scrape:${r.url}`))));
          if (!nativeFirst && nativeOn && hits.some((h) => !h || (h as any).error)) {
            const again = await Promise.all(reqs.map((r, i) => (hits[i] && !(hits[i] as any).error ? hits[i] : nativePage(r.url, r.page_num).catch(() => null))));
            again.forEach((a, i) => { if (a) hits[i] = a; });
          }
          if (hits.some((h) => !h || (h as any).error)) {
            const l = await missLive();
            if (l) return l;
          }
          const results = hits.map((h: any, i) => {
            if (h && !h.error) return { ...h, page_num: reqs[i].page_num ?? h.page_num };
            if (!h) want({ t: "scrape", url: reqs[i].url, page_num: reqs[i].page_num });
            // Past the cached pages: say "no more" (frontend stops scrolling) instead of showing an error.
            const first = (reqs[i].page_num ?? 1) <= 1;
            return h || {
              page: reqs[i].url, items: [], count: 0, next_page: null, page_num: reqs[i].page_num ?? 1,
              not_cached: true, ...(first ? { error: NOT_CACHED } : {}),
            };
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
            const l = await missLive();
            if (l) return l;
            if (nativeOn && !nativeFirst) { const n = await nativeSearch(sites, query); if (n) return n; }
            return json({ results: [], query, combined: [], count: 0, error: NOT_CACHED }, 200, headers);
          }
          const combined = parts.flatMap((p) => p.combined || []).sort((a, b) => (b._score || 0) - (a._score || 0));
          return json(
            { results: parts.flatMap((p) => p.results || []), query, combined, count: combined.length },
            200,
            headers
          );
        }

        case "/api/resolve":
        case "/api/resolve-full": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          if (!b.url) return err("url is required", 400, headers);
          const live = await tryLive();
          if (live) return live;
          const prefix = url.pathname === "/api/resolve" ? "resolve" : "resolve-full";
          const data = await kvGet(env.SCRAPE_DATA, `${prefix}:${b.url}`);
          if (!data || (data as any).error) { const l = await missLive(); if (l) return l; }
          if (!data) want({ t: "resolve", url: b.url, full: prefix === "resolve-full" });
          return json(data || { video: null, error: NOT_CACHED }, 200, headers);
        }

        case "/api/metadata": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          const u = (b.url || "").trim();
          if (!u) return err("valid url is required", 400, headers);
          // metadata is cheap and stable: shard cache first, live only on a miss
          const shard = (await sha1Hex(u))[0];
          const bundle = await kvGet<{ items?: Record<string, any> }>(env.SCRAPE_DATA, `meta-shard:${shard}`);
          const hit = bundle?.items?.[u] ?? (await kvGet<any>(env.SCRAPE_DATA, `meta:${u}`));
          if (hit) {
            const { _ts, ...meta } = hit;
            return json(meta, 200, headers);
          }
          const live = await viaBackend();
          if (live) return live;
          return json({ url: u, groups: [], error: NOT_CACHED }, 200, headers);
        }

        case "/api/livecams": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          const live = await tryLive();
          if (live) return live;
          const data = await kvGet(env.SCRAPE_DATA, `livecams:${b.url || "default"}`);
          if (!data || (data as any).error) { const l = await missLive(); if (l) return l; }
          return json(data || { items: [], count: 0, error: NOT_CACHED }, 200, headers);
        }

        case "/api/channels": {
          if (!isPost) break;
          const b = await body<{ category?: string | null; page?: number }>();
          const cat = b.category || "all";
          const page = Math.max(1, Number(b.page) || 1);
          const live = await tryLive();
          if (live) return live;

          // all pages live in ONE bundle key (1 KV write per scrape, however many pages there are)
          const bundle = await kvGet<{ pages?: Record<string, any> }>(env.SCRAPE_DATA, `channels-bundle:${cat}`);
          let data = bundle?.pages?.[String(page)] ?? (await kvGet(env.SCRAPE_DATA, `channels:${cat}:${page}`));
          if (!data || (data as any).error) {
            const l = await missLive();
            if (l) return l;
          }
          if (data && !(data as any).error) return json(data, 200, headers);

          if (page > 1) {
            // scrolled past the last cached page: that's the end of the list, not an error
            want({ t: "channels", category: cat === "all" ? null : cat, page });
            return json({ items: [], count: 0, next_page: null, page, end: true }, 200, headers);
          }
          want({ t: "channels", category: cat === "all" ? null : cat, page: 1 });
          return json({ items: [], count: 0, error: NOT_CACHED }, 200, headers);
        }

        case "/api/scrape-categories": {
          if (!isPost) break;
          const b = await body<{ url?: string; mode?: string; page_num?: number }>();
          if (!b.url) return err("url is required", 400, headers);
          // mirror Flask's mode detection
          let mode = b.mode === "pornstars" ? "models" : b.mode || "categories";
          try {
            const p = new URL(b.url);
            if (p.hostname.endsWith("freesexvideos.xxx") && p.pathname.toLowerCase().startsWith("/models")) mode = "models";
          } catch { /* keep mode */ }
          const live = await tryLive();
          if (live) return live;
          const data = await kvGet(env.SCRAPE_DATA, `categories:${b.url}:${mode}:${b.page_num || 1}`);
          if (!data || (data as any).error) { const l = await missLive(); if (l) return l; }
          if (!data) want({ t: "categories", url: b.url, mode, page_num: b.page_num || 1 });
          return json(data || { categories: [], count: 0, error: NOT_CACHED }, 200, headers);
        }

        case "/api/cam-thumb": {
          if (!isGet) break;
          const raw = (url.searchParams.get("u") || "").trim().replace(/[?&]_t=\d+$/, "");
          if (!raw) return err("u is required", 400, headers);
          let target: URL;
          try { target = new URL(raw); } catch { return err("bad url", 400, headers); }
          if (target.protocol !== "https:" || isBlockedHost(target.hostname)) return err("blocked url", 400, headers);
          // the provider's own origin is the Referer its CDN expects (recorded by the scraper)
          const map = await kvGet<{ origins?: Record<string, string> }>(env.SCRAPE_DATA, "cam-thumb-origins");
          const referer = map?.origins?.[raw] || target.origin + "/";
          const upstream = await fetch(target.toString(), {
            headers: {
              "User-Agent": "Mozilla/5.0",
              Accept: "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
              Referer: referer,
              Origin: referer.replace(/\/$/, ""),
            },
            cf: { cacheTtl: 30, cacheEverything: true },
          } as RequestInit);
          const type = (upstream.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
          if (!upstream.ok || !type.startsWith("image/")) {
            return new Response(null, { status: 404, headers: { ...headers, "Cache-Control": "public, max-age=30" } });
          }
          return new Response(upstream.body, {
            status: 200,
            headers: { ...headers, "Content-Type": type, "Cache-Control": "public, max-age=30" },
          });
        }

        case "/api/translate-titles":
          if (isPost) return json({ titles: [], count: 0 }, 200, headers);
          break;
      }
      return err("Not found", 404, headers);
    } catch (e) {
      return err(e instanceof Error ? e.message : "Internal error", 500, headers);
    }
  },
};
