# Porn Archive - Cloudflare Worker + KV + GitHub Actions

Nothing runs on your computer. `run.bat` pushes to GitHub; GitHub Actions deploys the Worker and the page and runs the scrapers.

## Files
| File | Role |
|---|---|
| `index.html` | the page (deployed to Cloudflare Pages, `__API_BASE__` is replaced with the Worker URL) |
| `src/index.ts` | the Worker: routing, KV cache, stale-while-revalidate, cron (cams every 10 min + watchdog) |
| `search.ts` | search over title + tags + genres + stars + studios + description (index, taxonomy pages, ranking) |
| `scrape.ts` `listings.ts` `resolve.ts` `metadata.ts` | live scrapers inside the Worker: feeds / search, categories / stars / studios, playable links, metadata |
| `cams.ts` `channels.ts` | live cams (platform feeds) and live TV (xlivetv) inside the Worker |
| `types.ts` `wrangler.toml` `package.json` `tsconfig.json` | Worker config |
| `scraper_kv.py` | the GitHub-Actions scraper that fills KV (feeds, listings, search, metadata, search + taxonomy index, cams, channels) |
| `scraper.py` `metadata.py` `livecams.py` `channels.py` `searchkit.py` | its libraries (`searchkit.py` = Python twin of `search.ts`) |
| `.github/workflows/` | `deploy.yml` (Worker + Pages, then one full scrape), `scrape-live.yml` (:11/:41), `scrape-full.yml` (every 6 h) |
| `.github/scripts/kv_ids.sh` | finds or creates the two KV namespaces |
| `run.bat` | commit + push = deploy (Windows) |

## One-time setup (repo Settings -> Secrets and variables -> Actions)
* Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`.
* Optional secret `GH_DISPATCH_TOKEN`: fine-grained token, this repo only, Actions: Read and write. Lets the Worker start the live scrape when data is stale.
* Optional variables: `WORKER_MODE` (`kv` = cache first), `ALLOWED_HOSTS`, `CHATURBATE_WM`, `CAM_PROVIDERS` (e.g. `chaturbate,cam4`).

## How it stays fresh
* Cams refresh inside Cloudflare (cron every 15 min + whenever a visitor hits stale data); GitHub is only a backup.
* Good data lives 30 days in KV and is served instantly while refreshed in the background; cams/channels never show an error while any copy exists.
* Searches that miss the cache are queued and scraped on the next run. Videos opened without metadata are queued too.
* `/healthz` and `/api/status` show data age and the last scraper report.

## Free-tier budget
KV allows 1,000 writes/day for the whole account: full scrape <= ~190 writes x 2 runs/day, the live run only re-stamps unchanged data every 6 h, cams <= ~96/day. A deploy no longer starts a full scrape by itself (Actions -> Deploy -> Run workflow does). If every write answers HTTP 429 the daily limit is used up: the scraper now stops after 3 refused writes and says so; it resets at 00:00 UTC. Free Workers also allow 10 ms CPU and 50 subrequests per request; the Workers Paid plan ($5/mo) removes that worry.

## Scraper knobs (env vars in the workflows)
`SEARCH_PAGES=3` `CHANNEL_PAGES=20` `CHANNEL_CATEGORIES=6` `MAX_WANTED=30` `INDEX_MAX=2500` `TAXONOMY_MAX=4000` `MAX_METADATA=400`
Manual run: Actions -> Scrape live -> Run workflow -> `only` = `live` or `wanted`.
