# Making the Cloudflare site behave like the always-running server

## What was wrong
* **Live TV: "Not in cache - run scraper"** - the scraper cached channel pages 1-3. Infinite scroll asked for page 4, KV had nothing, and the Worker returned an error. Now:
  * the scraper stores **all** channel pages in one key (`channels-bundle:all`, 1 write), and
  * a page past the end is answered as "end of list", not as an error.
* **Search while scrolling** - only page 1 of 8 preset queries was cached. Now the scraper also caches the following result pages (`SEARCH_PAGES`, default 3), and anything visitors ask for that is missing is queued (`wanted:queue`) and scraped on the next run (hourly :30 run, or the full 6-hourly run).

## Real scraping on search (no backend needed)
`src/scrape.ts` is a TypeScript port of the generic scraper in `scraper.py`. When you search a keyword, the Worker now fetches the sites' own search pages (the same 6 URL shapes, raced), parses the video cards and follows `next_page` while you scroll. Order of answers: your Flask backend (if `BACKEND_URL` is set) -> **Worker scrapes the site live** -> KV cache. Live answers are edge-cached (search 5 min, pages 10 min); failed/empty scrapes are never cached.
Set `LIVE_SCRAPE=off` (Worker variable) to disable it. It needs the `package.json` in the repo root (`node-html-parser`); the workflow already runs `npm install` when that file exists.
Limits: sites that need the special `sourcetest`/`extras` scrapers, title translation, and sites that block Cloudflare's IPs still need the Flask backend or the cache. A busy Worker on the free plan can hit the 10 ms CPU limit; the Workers Paid plan ($5/mo) removes that.

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
