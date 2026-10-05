export interface Env {
  CACHE: KVNamespace;
  SCRAPE_DATA: KVNamespace;
  CLOUDFLARE_API_TOKEN?: string;
  CLOUDFLARE_ACCOUNT_ID?: string;
  /** Live backend (your Flask app, e.g. behind a Cloudflare Tunnel). When set, the Worker asks it FIRST
   *  and only falls back to KV when it is offline / slow / errors. */
  BACKEND_URL?: string;
  /** "live" (default when BACKEND_URL is set) = backend first, KV fallback.  "kv" = KV first, backend on miss. */
  MODE?: string;
  /** "on" (default): the Worker scrapes the websites itself for search + scrolling. "off": backend/KV only. */
  LIVE_SCRAPE?: string;
}
