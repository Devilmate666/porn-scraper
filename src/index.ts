import { Env } from "./types";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "600",
};

function corsHeaders(origin?: string) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
  };
}

async function handleOptions(request: Request, env: Env): Promise<Response> {
  const origin = request.headers.get("Origin");
  return new Response(null, { headers: corsHeaders(origin) });
}

async function kvGet<T>(kv: KVNamespace, key: string): Promise<T | null> {
  const data = await kv.get(key, "json");
  return data as T | null;
}

async function jsonResponse(data: unknown, status = 200, headers: Record<string, string> = {}): Promise<Response> {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

async function errorResponse(message: string, status = 400, headers: Record<string, string> = {}): Promise<Response> {
  return jsonResponse({ error: message }, status, headers);
}

function getOrigin(request: Request): string {
  return request.headers.get("Origin") || "*";
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const origin = getOrigin(request);
    const headers = corsHeaders(origin);

    if (request.method === "OPTIONS") {
      return handleOptions(request, env);
    }

    try {
      switch (true) {
        case url.pathname === "/healthz" && request.method === "GET":
          return jsonResponse({ ok: true }, 200, headers);

        case url.pathname === "/api/catalog-urls" && request.method === "GET":
          return jsonResponse(await kvGet(env.SCRAPE_DATA, "catalog:urls") || {}, 200, headers);

        case url.pathname === "/api/test-sites" && request.method === "GET":
          return jsonResponse(await kvGet(env.SCRAPE_DATA, "test-sites") || { sites: [] }, 200, headers);

        case url.pathname === "/api/scrape" && request.method === "POST": {
          const body = await request.json() as { urls?: string[]; url?: string; fresh?: boolean };
          const rawUrls: any[] = (body as any).urls || (body.url ? [body.url] : []);
          const urls: string[] = rawUrls
            .map((u: any) => (typeof u === "string" ? u : u?.url))
            .filter(Boolean);
          if (!urls.length) return errorResponse("No urls provided", 400, headers);

          const results = await Promise.all(
            urls.slice(0, 12).map(async (u) => {
              const key = `scrape:${u}`;
              const cached = await kvGet(env.CACHE, key);
              if (cached && !body.fresh) return cached;
              const freshData = await kvGet(env.SCRAPE_DATA, key);
              if (freshData) {
                await env.CACHE.put(key, JSON.stringify(freshData), { expirationTtl: 3600 });
                return freshData;
              }
              return { page: u, items: [], count: 0, error: "Not in cache - run scraper", next_page: null, page_num: 1 };
            })
          );
          return jsonResponse({ results }, 200, headers);
        }

        case url.pathname === "/api/search" && request.method === "POST": {
          const body = await request.json() as { sites?: string[]; query?: string };
          const sites = (body.sites || []).filter(Boolean);
          const query = (body.query || "").trim().toLowerCase();
          if (!sites.length || !query) return errorResponse("sites and query are required", 400, headers);

          // One KV entry per (site, query) - must match scraper_kv.search_key()
          const parts = await Promise.all(sites.map(async (site) => {
            const key = `search:${site}:${query}`;
            const cached = await kvGet<any>(env.CACHE, key);
            if (cached) return cached;
            const data = await kvGet<any>(env.SCRAPE_DATA, key);
            if (data) await env.CACHE.put(key, JSON.stringify(data), { expirationTtl: 300 });
            return data;
          }));
          const found = parts.filter(Boolean);
          if (!found.length) {
            return jsonResponse({ results: [], query, combined: [], count: 0, error: "Not in cache - run scraper" }, 200, headers);
          }
          const results = found.flatMap((p: any) => p.results || []);
          const combined = found.flatMap((p: any) => p.combined || []);
          return jsonResponse({ results, query, combined, count: combined.length }, 200, headers);
        }

        case url.pathname === "/api/resolve" && request.method === "POST": {
          const body = await request.json() as { url?: string };
          const urlStr = body.url;
          if (!urlStr) return errorResponse("url is required", 400, headers);
          const data = await kvGet(env.SCRAPE_DATA, `resolve:${urlStr}`);
          return jsonResponse(data || { video: null, error: "Not in cache - run scraper" }, 200, headers);
        }

        case url.pathname === "/api/resolve-full" && request.method === "POST": {
          const body = await request.json() as { url?: string };
          const urlStr = body.url;
          if (!urlStr) return errorResponse("url is required", 400, headers);
          const data = await kvGet(env.SCRAPE_DATA, `resolve-full:${urlStr}`);
          return jsonResponse(data || { video: null, error: "Not in cache - run scraper" }, 200, headers);
        }

        case url.pathname === "/api/metadata" && request.method === "POST": {
          const body = await request.json() as { url?: string };
          const urlStr = body.url;
          if (!urlStr) return errorResponse("valid url is required", 400, headers);
          const data = await kvGet(env.SCRAPE_DATA, `meta:${urlStr}`);
          return jsonResponse(data || { url: urlStr, groups: [], error: "Not in cache - run scraper" }, 200, headers);
        }

        case url.pathname === "/api/livecams" && request.method === "POST": {
          const body = await request.json() as { url?: string; force?: boolean };
          const data = await kvGet(env.SCRAPE_DATA, `livecams:${body.url || "default"}`);
          return jsonResponse(data || { items: [], count: 0, error: "Not in cache - run scraper" }, 200, headers);
        }

        case url.pathname === "/api/channels" && request.method === "POST": {
          const body = await request.json() as { category?: string; page?: number; force?: boolean };
          const key = `channels:${body.category || "all"}:${body.page || 1}`;
          const data = await kvGet(env.SCRAPE_DATA, key);
          return jsonResponse(data || { items: [], count: 0, error: "Not in cache - run scraper" }, 200, headers);
        }

        case url.pathname === "/api/cam-thumb" && request.method === "GET": {
          const thumbUrl = url.searchParams.get("u");
          if (!thumbUrl) return errorResponse("u is required", 400, headers);
          return new Response(null, { status: 404, headers: { ...headers, "Cache-Control": "public, max-age=30" } });
        }

        case url.pathname === "/api/translate-titles" && request.method === "POST": {
          return jsonResponse({ titles: [], count: 0 }, 200, headers);
        }

        case url.pathname === "/api/scrape-categories" && request.method === "POST": {
          const body = await request.json() as { url?: string; mode?: string; page_num?: number };
          const urlStr = body.url;
          if (!urlStr) return errorResponse("url is required", 400, headers);
          let mode = body.mode === "pornstars" ? "models" : (body.mode || "categories");
          try {
            const p = new URL(urlStr);
            if (p.hostname.endsWith("freesexvideos.xxx") && p.pathname.toLowerCase().startsWith("/models")) mode = "models";
          } catch {}
          const key = `categories:${urlStr}:${mode}:${body.page_num || 1}`;
          const data = await kvGet(env.SCRAPE_DATA, key);
          return jsonResponse(data || { categories: [], count: 0, error: "Not in cache - run scraper" }, 200, headers);
        }

        default:
          return errorResponse("Not found", 404, headers);
      }
    } catch (e) {
      return errorResponse(e instanceof Error ? e.message : "Internal error", 500, headers);
    }
  },
};