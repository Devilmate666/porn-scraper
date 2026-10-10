export interface Env {
  CACHE: KVNamespace;
  SCRAPE_DATA: KVNamespace;
  /** D1 database: accounts, login codes, sessions, synced favorites (optional: login is off without it) */
  DB?: D1Database;
  /** Random 32+ char secret that signs login codes (wrangler secret AUTH_SECRET) */
  AUTH_SECRET?: string;
  /** Email provider: set ONE of these (secrets) plus MAIL_FROM */
  RESEND_API_KEY?: string;
  BREVO_API_KEY?: string;
  /** Sender, e.g. "Archive <login@yourdomain.com>" (must be verified at the provider) */
  MAIL_FROM?: string;
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
  /** Chaturbate affiliate campaign slug for the fallback endpoint (default "dvafl"). */
  CHATURBATE_WM?: string;
  /** Comma list of direct cam platforms to fetch, default "chaturbate,stripchat,cam4,camsoda". */
  CAM_PROVIDERS?: string;
  /** Optional proxy for Lemoncams scraping (e.g., "https://r.jina.ai/http://"). */
  LEMONCAMS_PROXY?: string;
  /** Watchdog: lets the Worker's cron start the GitHub scraper workflow when data is stale. */
  GITHUB_TOKEN?: string;      // fine-grained PAT with Actions: read & write (set as secret)
  GITHUB_REPO?: string;       // "owner/name"
  GITHUB_REF?: string;        // default "main"
  GITHUB_WORKFLOW?: string;   // default "scrape-live.yml"
}
