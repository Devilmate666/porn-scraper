export interface Env {
  CACHE: KVNamespace;
  SCRAPE_DATA: KVNamespace;
  CLOUDFLARE_API_TOKEN?: string;
  CLOUDFLARE_ACCOUNT_ID?: string;
  /** Optional live backend (your Flask app). Used when KV has no cached answer. */
  BACKEND_URL?: string;
}
