export interface Env {
  CACHE: KVNamespace;
  SCRAPE_DATA: KVNamespace;
  CLOUDFLARE_API_TOKEN?: string;
  CLOUDFLARE_ACCOUNT_ID?: string;
  /** Live backend (your Flask app, e.g. behind a Cloudflare Tunnel). Asked FIRST when set; KV/native are the fallbacks. */
  BACKEND_URL?: string;
  /** "live" (default) = backend/native first, KV fallback.  "kv" = KV first. */
  MODE?: string;
  /** "on" (default): the Worker scrapes the websites itself. "off": backend/KV only. */
  LIVE_SCRAPE?: string;
  /** Extra hosts the Worker may scrape on request, comma separated (the 4 built-in sources + scraper catalog are always allowed). */
  ALLOWED_HOSTS?: string;
  /** "1" = allow any public host (disables the allowlist). */
  ALLOW_ANY?: string;
  /** Max native scrapes per client IP per minute (default 90). */
  RATE_PER_MIN?: string;
  /** Watchdog: lets the Worker's cron start the GitHub scraper workflow when data is stale. */
  GITHUB_TOKEN?: string;      // fine-grained PAT with Actions: read & write (set as secret)
  GITHUB_REPO?: string;       // "owner/name"
  GITHUB_REF?: string;        // default "main"
  GITHUB_WORKFLOW?: string;   // default "scrape-live.yml"
}
