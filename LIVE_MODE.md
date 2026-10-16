# Reliability: why cams / live TV showed "Not in cache" and how it is fixed

## Causes found
1. Cams and channels were stored in KV for **30 minutes**, but the scraper only ran about once an hour and GitHub often delays or skips scheduled runs, so the key expired -> "Not in cache". The next successful run "brought it back by itself".
2. "Skip write if identical" did not refresh the expiry, so unchanged data (typical for channels) expired even after a good scrape.
3. The schedule used :00 and :30, the minutes where GitHub drops the most cron runs.
4. The live cron shared a concurrency group with the 40-minute full scrape, so queued live runs were dropped.
5. Cam platforms sometimes refuse GitHub's IPs; one failed run used to leave a hole.
6. In a full run one exception in an early job aborted everything after it (cams/channels came late in the order).

## Fixes (backend only)
Worker (`src/`)
* Good data lives 30 days; the Worker serves **stale data instantly** and refreshes in the background (stale-while-revalidate). Cams/channels never return an error while any copy exists (KV of any age, or a 3-day last-known-good copy at the edge).
* **Cams refresh themselves inside Cloudflare** (`cams.ts`): on a cron trigger every 10 minutes and whenever a visitor hits stale data. Each platform is independent, a platform that fails keeps its previous cams. Cams no longer depend on GitHub at all.
* **Watchdog**: the cron starts the GitHub live-scrape workflow when channels/cams are stale or visitor requests are queued (optional, needs the token below).
* Negative caching (45 s) so a blocking site is not hammered, in-flight de-duplication, a 6-connection limiter, one retry with jitter on 429/5xx, bot-challenge detection, per-IP rate limit (`RATE_PER_MIN`, default 90/min).
* SSRF protection: URLs sent by the browser must belong to the known sources (4 built-in + hosts in the scraper catalog + `ALLOWED_HOSTS`); private/localhost addresses are never fetched.
* `/healthz` and `/api/status` show data age, queue size and the last scraper report. Response headers `X-Source` and `X-Cache-Age` show where an answer came from.

Scraper (`scraper_kv.py`)
* Every good value: 30-day TTL; live keys carry `_ts`. An error or **empty** result never overwrites good data; a KV outage never overwrites anything.
* "Skip identical" only when the key still has > 14 days left (expiry read once per run).
* Retries (cams/channels 3x, KV API 5x with backoff), a per-run write budget (live keys exempt), a time budget so runs end cleanly, every job isolated (`safe_job`), perishable jobs (cams/channels) run first, a `scrape-status` report for `/healthz`.
* A live run where both cams and channels failed ends **red** in GitHub (you get the email) while the old data keeps being served.

Workflows (`.github/`)
* Split into `deploy.yml`, `scrape-live.yml` (:11/:41, own concurrency group, 15-min limit), `scrape-full.yml` (every 6 h at :17, own group). Deploy never blocks scraping and vice versa.
* Keep-alive step so GitHub never disables the schedules after 60 days of inactivity.
* Smoke test after deploy.

## One-time setup for the watchdog (optional but recommended)
GitHub -> Settings -> Developer settings -> Fine-grained tokens -> new token, **only this repo**, permission **Actions: Read and write**. Save it as repository secret `GH_DISPATCH_TOKEN`. The next deploy hands it to the Worker. Without it everything else still works, cams still self-heal.
Optional repo variables: `ALLOWED_HOSTS` (more scrapable hosts), `WORKER_MODE=kv`.

# Making the Cloudflare site behave like the always-running server

## What was wrong
* **Live TV: "Not in cache - run scraper"** - the scraper cached channel pages 1-3. Infinite scroll asked for page 4, KV had nothing, and the Worker returned an error. Now:
  * the scraper stores **all** channel pages in one key (`channels-bundle:all`, 1 write), and
  * a page past the end is answered as "end of list", not as an error.
* **Search while scrolling** - only page 1 of 8 preset queries was cached. Now the scraper also caches the following result pages (`SEARCH_PAGES`, default 3), and anything visitors ask for that is missing is queued (`wanted:queue`) and scraped on the next run (hourly :30 run, or the full 6-hourly run).

## What is scraped live by the Worker (no backend needed)
| Endpoint | Live scraper |
|---|---|
| `/api/search`, `/api/scrape` (all video feeds, networks/categories/pornstar feeds, scrolling) | `src/scrape.ts` |
| `/api/scrape-categories` (categories, pornstars, networks/studios, series, tags) | `src/listings.ts` |
| `/api/resolve`, `/api/resolve-full` (playable video links) | `src/resolve.ts` |
| `/api/metadata` (duration, stars, tags ...) | `src/metadata.ts` |
| `/api/livecams`, `/api/channels` | still cron/backend only (needs `livecams.py` / `channels.py`) |

## Real scraping on search (no backend needed)
`src/scrape.ts` is a TypeScript port of the generic scraper in `scraper.py`. When you search a keyword, the Worker now fetches the sites' own search pages (the same 6 URL shapes, raced), parses the video cards and follows `next_page` while you scroll. Order of answers: your Flask backend (if `BACKEND_URL` is set) -> **Worker scrapes the site live** -> KV cache. Live answers are edge-cached (search 5 min, pages 10 min); failed/empty scrapes are never cached.
Set `LIVE_SCRAPE=off` (Worker variable) to disable it. It needs the `package.json` in the repo root (`node-html-parser`); the workflow already runs `npm install` when that file exists.
Limits: sites that block Cloudflare's IPs still need the Flask backend or the cache. A busy Worker on the free plan can hit the 10 ms CPU limit; the Workers Paid plan ($5/mo) removes that.

## Real live behaviour (recommended): hybrid
GitHub Actions can only pre-fetch. To get a truly live server, point the Worker at your Flask app:

1. Install `cloudflared` on the machine that runs `app.py`.
2. Run `./run_live.sh`. It starts Flask and a tunnel and prints a `https://....trycloudflare.com` URL.
3. Add that URL as the GitHub secret `BACKEND_URL` and re-run the workflow (this redeploys the Worker with `BACKEND_URL`).
   For a URL that never changes use a named tunnel (`cloudflared tunnel create ...`, route a hostname to it, then `TUNNEL=<name> ./run_live.sh`).

With `BACKEND_URL` set the Worker:
* asks Flask **first** (28 s timeout for scrape/search), so search, scroll, live TV and cams are fresh like locally;
* keeps good answers in the colo's Cache API (5-10 min, no KV writes), like Flask's own caches;
* never serves or caches a failed/empty scrape - it falls back to KV instead;
* falls back to KV automatically when your computer is off.

`/healthz` shows `{"live": true, "mode": "live-first"}`. Set repo variable `WORKER_MODE=kv` to prefer the cache instead.

## No always-on machine?
Host the same Flask app on any free container host (Render, Fly, Koyeb ...) and use its URL as `BACKEND_URL`. Without any backend the site works from cache only: more depth is pre-scraped and misses are queued, but a brand-new search can only appear after the next scraper run.

## Scraper knobs (env vars)
`SEARCH_PAGES=3` `CHANNEL_PAGES=20` `CHANNEL_CATEGORIES=6` `MAX_WANTED=30` `WANTED_SEARCH_PAGES=5` `WANTED_FEED_PAGES=3`
Manual runs: Actions -> Run workflow -> `only` = `live` or `wanted`.
