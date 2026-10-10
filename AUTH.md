# Username/password login + favorites sync

Username + password (no email provider needed). The visitor creates an account, signs in, and favorites sync between devices.
Runs only in the Worker; Flask is not involved. Without setup the site is unchanged
(the "Sign in" button simply does not appear).

## Where each file goes (same layout as your repo)
| File | Repo path | New / changed |
|---|---|---|
| `auth.ts` | `auth.ts` (next to `search.ts`) | new |
| `src/index.ts` | `src/index.ts` | `/api/auth/*` + `/api/sync` routes, `Authorization` allowed in CORS, cron cleanup |
| `types.ts` | `types.ts` | new env fields |
| `wrangler.toml` | `wrangler.toml` | D1 binding `DB` |
| `migrations/0001_auth.sql` | `migrations/0001_auth.sql` | new (tables) |
| `migrations/0002_alter_users.sql` | `migrations/0002_alter_users.sql` | migrates an OLD users table (email + login_codes) to the new schema |
| `.github/scripts/d1_id.sh` | same | new (finds/creates the D1 database) |
| `.github/workflows/deploy.yml` | same | D1 step, both migrations, login secret |
| `index.html` | `index.html` | favorites keep deletion records, new Account script + button |

## One-time setup
1. **Cloudflare API token**: add the permission **D1: Edit** to your existing `CLOUDFLARE_API_TOKEN`
   (dash.cloudflare.com/profile/api-tokens). Without it the deploy still works, but login stays off (you get a warning).
2. **GitHub repo secret**: `AUTH_SECRET` (any random string of 32+ chars, e.g. `openssl rand -hex 32`).
3. Push with `run.bat`. The deploy creates the database `porn-archive-db`, creates the tables, and hands the secret to the Worker.

Check: `POST <worker>/api/auth/config` returns `{"enabled":true}`.
Never change `AUTH_SECRET` after launch unless you accept that pending sessions stop working (they expire after 90 days anyway).

## How sync behaves
* Every heart tap is one change; the page pushes it ~1.5 s later. It also syncs on page load, on tab focus, when back online and every 5 min.
* Per item, the newest timestamp wins. Removing a favorite is remembered (a deletion record, kept 90 days) so it also disappears on your other devices.
* Favorites saved before signing in are merged into the account on the first sign-in.
* **Sign out** pushes the last changes, then clears the favorites on that device (they stay in the account). Signing in as a different account never mixes the two.
* Limits: 5,000 favorites per account, 8 KB per favorite, 500 changes per request.

## Security notes
* Passwords are stored as PBKDF2-SHA256 hashes (250,000 iterations + random per-password salt). Session tokens are random 256-bit values; only their SHA-256 hash is stored.
* Rate limits (kept in D1): 10 registrations/hour per IP, 3/hour per username, 30 logins/10 min per IP, 10/10 min per username, 120 syncs/min per account.
* The session token is kept in `localStorage` (the page and the API are on different domains, so a cookie would be blocked by Safari/Firefox).
  That means any XSS bug on the page could read it; the page already escapes what it renders, keep doing that.
* "Delete account" removes the user, sessions and all synced data.

## Free-tier cost
D1 free plan: 100,000 row writes and 5 million row reads per day, 5 GB. One heart tap = ~3 row writes. KV is not used, so the 1,000 KV writes/day budget is untouched.

## Later: syncing more than favorites
`KINDS` in `auth.ts` lists the data kinds the server accepts (`fav` now). Add `"history"` or `"settings"`, then call
`/api/sync` with that `kind` from the page; the table and the conflict rules are generic.