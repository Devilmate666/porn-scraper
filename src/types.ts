export interface Env {
  CACHE: KVNamespace;
  SCRAPE_DATA: KVNamespace;
  CLOUDFLARE_API_TOKEN?: string;
  CLOUDFLARE_ACCOUNT_ID?: string;
}