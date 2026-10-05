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
          const urls = body.urls || (body.url ? [body.url] : []);
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
          const body = await request.json() as { sites?: string[]; query?: string; verify?: boolean; max_items?: number };
          const sites = (body.sites || []).filter(Boolean);
          const query = (body.query || "").trim();
          if (!sites.length || !query) return errorResponse("sites and query are required", 400, headers);

          const cacheKey = `search:${sites.sort().join(",")}:${query}:${body.verify}:${body.max_items || 40}`;
          const cached = await kvGet(env.CACHE, cacheKey);
          if (cached) return jsonResponse(cached, 200, headers);

          const data = await kvGet(env.SCRAPE_DATA, cacheKey);
          if (data) {
            await env.CACHE.put(cacheKey, JSON.stringify(data), { expirationTtl: 300 });
            return jsonResponse(data, 200, headers);
          }
          return jsonResponse({ results: [], query, combined: [], count: 0, error: "Not in cache - run scraper" }, 200, headers);
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
          const data = await kvGet(env.SCRAPE_DATA, `livecams:${body.url || "default"}:${body.force}`);
          return jsonResponse(data || { items: [], count: 0, error: "Not in cache - run scraper" }, 200, headers);
        }

        case url.pathname === "/api/channels" && request.method === "POST": {
          const body = await request.json() as { category?: string; page?: number; force?: boolean };
          const key = `channels:${body.category || "all"}:${body.page || 1}:${body.force}`;
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
          const key = `categories:${urlStr}:${body.mode || "categories"}:${body.page_num || 1}`;
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