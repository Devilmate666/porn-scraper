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
| `index.html` | `index.html` | favorites keep deletion records, new Account script + button, "Admin panel" button for the admin |
| `admin.ts` | `admin.ts` (next to `auth.ts`) | new: admin API |
| `admin.html` | `admin.html` (root, next to `index.html`) | new: the admin page, deployed to Pages as `/admin.html` |
| `migrations/0004_admin.sql` | `migrations/0004_admin.sql` | new: admin activity log |

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

## Admin panel (`/admin.html`)
Open `https://porn-archive.pages.dev/admin.html`, or sign in on the main site as the admin and press **Admin panel** in your account window.

**Who is the admin:** the account whose username is `ADMIN_USERNAME` in `wrangler.toml` (set to `HISHAM`). It is a normal account:
**register it yourself, right after the first deploy** (until you do, anyone could register that name and become admin).
The password is whatever you choose at registration; it is not stored anywhere in the code or the repo.
Other accounts can be made admins from the panel (the main admin cannot be demoted or deleted).

What you can see and do:
* Totals: users, new/active in 24 h and 7 d, open sessions, favorites.
* All users (search, sort, pages): username, id, created, last login, number of favorites and open sessions.
* One user: every favorite (title, site, link, date), every session (start, expiry, device), removed favorites count.
* Actions: sign a user out everywhere, grant/remove admin, delete the account and its data, export one user as JSON, export all users as CSV, clear login rate limits.
* Activity log of every admin action (who, what, which user, from which IP).

What you can NOT see: passwords. They are stored only as salted hashes, so nobody can read them (including you). If someone forgets theirs, the only options are deleting the account or adding a password-reset action later.

How it is protected:
* Every admin call is checked on the server (valid session AND admin account). The page being public does not matter: without an admin session every call returns 401/403.
* The page builds everything with `textContent` (a user cannot inject script through a favorite title) and has a Content-Security-Policy that only allows talking to your API. It is sent with `X-Frame-Options: DENY`, `Cache-Control: no-store`, `noindex`.
* Admin requests are rate limited per IP.
* Be aware that you can read users' favorites: mention it in your privacy notice.


## One account per person
* **In the browser:** once a browser has signed up or signed in, the "Create an account" button is gone for good, also after signing out (only "Sign in" remains). Deleting your own account brings it back.
* **On the server:** each network (IP address) may create `SIGNUP_MAX_PER_IP` accounts per `SIGNUP_WINDOW_DAYS` (default 1 per 30 days, set in `wrangler.toml`; `0` = unlimited). Only a keyed hash of the IP is stored, and rows are deleted after the window. The admin username is exempt.
* This is the best a website can do without verifying real identities: someone on another network (mobile data, VPN) can still register again, and people who share one network (family, school) share the limit. If real users get blocked, raise `SIGNUP_MAX_PER_IP`.
* Needs `migrations/0005_signups.sql` (the deploy runs it). Before it has run, nobody is blocked.
