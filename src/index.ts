import { Env } from "./types";

// ---------------------------------------------------------------------------
// Read-only API over KV. The scraper (GitHub Actions) writes into SCRAPE_DATA;
// key formats below MUST match scraper_kv.py.
//   scrape:{url}                       search:{site}:{query-lowercased}
//   categories:{url}:{mode}:{page}     livecams:{url|default}
//   channels:{category|all}:{page}     resolve:{url} / resolve-full:{url}
//   meta-shard:{sha1(url)[0]} -> { items: { url: metadata } }     cam-thumb-origins -> { origins: { img: referer } }
// KV reads use cacheTtl (edge cache) instead of writing to KV, which keeps us
// well under the free-tier write limit.
// ---------------------------------------------------------------------------

const EDGE_TTL = 300;
const NOT_CACHED = "Not in cache - run scraper";

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

    // Optional live backend (Flask): used only when KV has no cached answer.
    const backend = (env.BACKEND_URL || "").replace(/\/$/, "");
    const viaBackend = async (): Promise<Response | null> => {
      if (!backend) return null;
      try {
        const r = await fetch(backend + url.pathname + url.search, {
          method: request.method,
          headers: { "Content-Type": "application/json" },
          body: isPost ? bodyText : undefined,
        });
        if (!r.ok) return null;
        const out = new Response(r.body, r);
        for (const [k, v] of Object.entries(headers)) out.headers.set(k, v);
        return out;
      } catch { return null; }
    };

    try {
      switch (url.pathname) {
        case "/healthz":
          if (isGet) return json({ ok: true }, 200, headers);
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
          const urls: string[] = raw.map((u: any) => (typeof u === "string" ? u : u?.url)).filter(Boolean);
          if (!urls.length) return err("No urls provided", 400, headers);

          const hits = await Promise.all(urls.slice(0, 12).map((u) => kvGet(env.SCRAPE_DATA, `scrape:${u}`)));
          if (hits.some((h) => !h || (h as any).error)) {
            const live = await viaBackend();
            if (live) return live;
          }
          const results = hits.map(
            (h, i) => h || { page: urls[i], items: [], count: 0, error: NOT_CACHED, next_page: null, page_num: 1 }
          );
          return json({ results }, 200, headers);
        }

        case "/api/search": {
          if (!isPost) break;
          const b = await body<{ sites?: string[]; query?: string }>();
          const sites = (b.sites || []).filter(Boolean);
          const query = (b.query || "").trim().toLowerCase();
          if (!sites.length || !query) return err("sites and query are required", 400, headers);

          const parts = (
            await Promise.all(sites.map((s) => kvGet<any>(env.SCRAPE_DATA, `search:${s}:${query}`)))
          ).filter(Boolean) as any[];
          if (!parts.length) {
            const live = await viaBackend();
            if (live) return live;
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
          const prefix = url.pathname === "/api/resolve" ? "resolve" : "resolve-full";
          const data = await kvGet(env.SCRAPE_DATA, `${prefix}:${b.url}`);
          if (!data) { const live = await viaBackend(); if (live) return live; }
          return json(data || { video: null, error: NOT_CACHED }, 200, headers);
        }

        case "/api/metadata": {
          if (!isPost) break;
          const b = await body<{ url?: string }>();
          const u = (b.url || "").trim();
          if (!u) return err("valid url is required", 400, headers);
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
          const data = await kvGet(env.SCRAPE_DATA, `livecams:${b.url || "default"}`);
          if (!data) { const live = await viaBackend(); if (live) return live; }
          return json(data || { items: [], count: 0, error: NOT_CACHED }, 200, headers);
        }

        case "/api/channels": {
          if (!isPost) break;
          const b = await body<{ category?: string | null; page?: number }>();
          const data = await kvGet(env.SCRAPE_DATA, `channels:${b.category || "all"}:${b.page || 1}`);
          if (!data) { const live = await viaBackend(); if (live) return live; }
          return json(data || { items: [], count: 0, error: NOT_CACHED }, 200, headers);
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
          const data = await kvGet(env.SCRAPE_DATA, `categories:${b.url}:${mode}:${b.page_num || 1}`);
          if (!data) { const live = await viaBackend(); if (live) return live; }
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
