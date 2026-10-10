# Email login + favorites sync

Passwordless: the visitor types an email, gets a 6-digit code (10 min, 5 tries), and is signed in for 90 days.
Favorites then sync between devices. Runs only in the Worker; Flask is not involved. Without setup the site is unchanged
(the "Sign in" button simply does not appear).

## Where each file goes (same layout as your repo)
| File | Repo path | New / changed |
|---|---|---|
| `auth.ts` | `auth.ts` (next to `search.ts`) | new |
| `src/index.ts` | `src/index.ts` | `/api/auth/*` + `/api/sync` routes, `Authorization` allowed in CORS, cron cleanup |
| `types.ts` | `types.ts` | new env fields |
| `wrangler.toml` | `wrangler.toml` | D1 binding `DB` |
| `migrations/0001_auth.sql` | `migrations/0001_auth.sql` | new (tables) |
| `.github/scripts/d1_id.sh` | same | new (finds/creates the D1 database) |
| `.github/workflows/deploy.yml` | same | D1 step, migration, login secrets |
| `index.html` | `index.html` | favorites keep deletion records, new Account script + button |

## One-time setup
1. **Cloudflare API token**: add the permission **D1: Edit** to your existing `CLOUDFLARE_API_TOKEN`
   (dash.cloudflare.com/profile/api-tokens). Without it the deploy still works, but login stays off (you get a warning).
2. **Email provider** (pick one, both have free tiers):
   * **Resend**: create an API key, and verify a domain you own (without a domain it can only email yourself).
   * **Brevo**: create an API key and verify a single sender address (no domain needed, deliverability is weaker).
3. **GitHub repo secrets**: `AUTH_SECRET` (any random string of 32+ chars, e.g. `openssl rand -hex 32`) and
   `RESEND_API_KEY` **or** `BREVO_API_KEY`.
4. **GitHub repo variable** `MAIL_FROM`, e.g. `Archive <login@yourdomain.com>` (must be the verified sender).
5. Push with `run.bat`. The deploy creates the database `porn-archive-db`, creates the tables, and hands the secrets to the Worker.

Check: `POST <worker>/api/auth/config` returns `{"enabled":true}`.
Never change `AUTH_SECRET` after launch unless you accept that pending codes stop working (sessions are unaffected).

## How sync behaves
* Every heart tap is one change; the page pushes it ~1.5 s later. It also syncs on page load, on tab focus, when back online and every 5 min.
* Per item, the newest timestamp wins. Removing a favorite is remembered (a deletion record, kept 90 days) so it also disappears on your other devices.
* Favorites saved before signing in are merged into the account on the first sign-in.
* **Sign out** pushes the last changes, then clears the favorites on that device (they stay in the account). Signing in as a different account never mixes the two.
* Limits: 5,000 favorites per account, 8 KB per favorite, 500 changes per request.

## Security notes
* Codes are stored only as HMAC hashes, session tokens only as SHA-256 hashes. Wrong-code attempts are counted atomically (max 5 per code).
* Rate limits (kept in D1): 6 codes/hour per address, 12/hour per IP, 40 code checks per 10 min per IP, 120 syncs/min per account.
* The session token is kept in `localStorage` (the page and the API are on different domains, so a cookie would be blocked by Safari/Firefox).
  That means any XSS bug on the page could read it; the page already escapes what it renders, keep doing that.
* "Delete account" removes the user, sessions and all synced data.
* You are now storing email addresses. Keep them out of logs and tell users what they are used for.

## Free-tier cost
D1 free plan: 100,000 row writes and 5 million row reads per day, 5 GB. One heart tap = ~3 row writes. KV is not used, so the 1,000 KV writes/day budget is untouched.

## Later: syncing more than favorites
`KINDS` in `auth.ts` lists the data kinds the server accepts (`fav` now). Add `"history"` or `"settings"`, then call
`/api/sync` with that `kind` from the page; the table and the conflict rules are generic.
