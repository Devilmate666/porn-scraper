# What changed (only these files differ from your upload; everything else is untouched)

New: `src/search.ts`, `searchkit.py`   Changed: `src/index.ts`, `src/scrape.ts`, `src/cams.ts`, `src/types.ts`,
`scraper_kv.py`, `livecams.py`, `app.py`, `deploy.yml`, `scrape-full.yml`, `scrape-live.yml`. No UI/index.html changes.

## 1. Search now looks at everything, not only titles
A keyword is matched against title, tags, genres/categories, pornstars, studios/series, description and the URL slug
(weights: title/exact tag > category/star > studio > description/url; plurals folded; multi-word needs >= half the words).
Three sources feed ONE ranked `combined` list (each item gets `_score`, `_match` = which fields hit, `_via` = which source):
1. the site's own search page (shape remembered per site by *id*; the old index-based memory broke on multi-word queries)
2. **taxonomy pages**: if the keyword is (part of) a category/tag/star/studio name, that page's feed is used
   (pre-scraped `scrape:` copy first, else 1 live request). Unknown keywords try the site's learnt URL shape once.
3. **search index** (`search-index` KV key): every video the scraper has seen + tags/genres/stars/studios from its metadata
   + the category feed / keyword it was found under. No network cost; answers "brazzers", "jane doe", "anal" even when no
   title says it, and replaces "Not in cache" for any keyword the index knows.
Works the same with Flask (`BACKEND_URL`): its answer is merged with taxonomy + index hits and re-ranked.
Old ranking let "has a thumbnail" (+10) outweigh a title match (+8); now quality is at most +5.

## 2. Live cams
* Room detection picked the LONGEST list with a `name`, so a category/filter list could replace the real rooms (fixed, both languages).
* Pages of a platform were fetched one after another (3 x 12 s); now in parallel, one jittered retry on 429/5xx/timeout.
* HTML bot-challenge pages (status 200) are reported as "bot challenge" instead of a JSON parse error.
* **Carry-over never expired**: a failed platform's cams were re-carried on every refresh because the payload age reset on each
  write. Each cam now has `_seen`; carried cams expire after 3 h. GitHub's run also carries over instead of replacing the
  Worker's richer copy with a poorer one.
* Cron refreshes every tick (stale threshold 8 min, was 20), visitors trigger it after 10 min.
* `/api/livecams` passes the KV text straight through (no 500 KB parse + stringify per request), LKG copy written <= once / 5 min.
* `platform_status` per platform in the payload and `/api/livecams`; optional vars `CHATURBATE_WM`, `CAM_PROVIDERS`.
* GitHub's live run skips cams/channels it does not need to rewrite (saves KV writes).

## 3. Caching / metadata / performance
* Metadata requests that miss are queued (`{t:"meta"}`); the next run fetches them first into the shards and the index.
* Big KV values (metadata shards, channel bundle, index) are parsed once per isolate (`memoKV`), not per request.
* `/api/cam-thumb` was an open image proxy for any https host: now limited to cam/TV image CDNs, scraper-recorded URLs and allowed sites.
* Free-tier KV budget: full-run `MAX_KV_WRITES` 450 -> 180 (4 runs x 450 exceeded 1,000 writes/day).
* `scrape-live.yml`: the manual `only` input no longer goes into the shell line.
* Scraper cache key = Worker key (`queryKey`: trim, lower-case, collapse spaces).

## Needs your attention
* Deploy `searchkit.py` next to `scraper_kv.py`/`app.py` (repo root). First full run builds `taxonomy-index` + `search-index`;
  until then search behaves as before plus the new ranking.
* I could not reach the real sites from here and `sourcetest.py`, `extras.py`, `translate_titles.py`, `scraper.py` internals were
  not changed/tested live. Tests run: typecheck, mocked Worker search/cams flows, scraper logic on a fake KV.
* Cam platforms (esp. Chaturbate/Stripchat) may still refuse Cloudflare/GitHub IPs; `platform_status` / `diagnostics` now say which and why.
* Free Workers plan: 50 subrequests and 10 ms CPU per request. First search on a new site tries several URL shapes; later ones use 1.
