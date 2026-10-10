// ---------------------------------------------------------------------------------------------------------
// Username/password login + per-account data sync, stored in Cloudflare D1.
//
//   POST /api/auth/config    -> { enabled }                       (the page hides the account button when false)
//   POST /api/auth/register  { username, password }  -> { token, user, expires }
//   POST /api/auth/login     { username, password }  -> { token, user, expires }
//   POST /api/auth/me        (Bearer)                -> { user }
//   POST /api/auth/logout    (Bearer) { all? }       -> { ok }       all:true ends every session of the account
//   POST /api/auth/delete    (Bearer) { confirm: username } -> { ok } deletes the account and all its data
//   POST /api/sync           (Bearer) { kind, cursor, changes:[{id, ts, data|null}] } -> { changes, cursor, more }
//
// Why a password and not email: no email provider needed, works instantly, no deliverability issues.
// Passwords are stored as Argon2id hashes; sessions are random 256-bit tokens sent as "Authorization: Bearer";
// only their SHA-256 hash is stored.
// Why D1 and not KV: KV's free tier allows 1,000 writes/day for everything; D1 allows 100,000 row writes/day.
// ---------------------------------------------------------------------------------------------------------
import { Env } from "./types";

const SESSION_TTL_S = 90 * 86400;
const MAX_SESSIONS = 10;                   // per account; the oldest are dropped
const MAX_ITEMS = 5000;                    // live (not deleted) items per account and kind
const MAX_DATA_BYTES = 8 * 1024;           // one item
const MAX_CHANGES_PER_CALL = 500;
const PULL_LIMIT = 1000;
const TOMBSTONE_KEEP_S = 90 * 86400;
const KINDS = new Set(["fav"]);            // add "history", "settings"... here when the page starts syncing them
const USERNAME_RX = /^[a-zA-Z0-9_]{3,32}$/;
const PASSWORD_MIN = 6;

type Out = (data: unknown, status?: number, extra?: Record<string, string>) => Response;
const nowS = () => Math.floor(Date.now() / 1000);

// ------------------------------------------------------------------ crypto helpers
const enc = new TextEncoder();
const hex = (b: ArrayBuffer | Uint8Array) => [...new Uint8Array(b as ArrayBuffer)].map((x) => x.toString(16).padStart(2, "0")).join("");
const b64url = (b: Uint8Array) => btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function sha256Hex(s: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
function randomToken(): string {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return b64url(b);
}

// ------------------------------------------------------------------ password hashing
// Argon2id via WebCrypto is not available in Cloudflare Workers, so we use PBKDF2 with a high
// iteration count + a random per-password salt. Good enough against offline cracking for a
// small community site; if you ever need stronger, swap in argon2 (it runs server-side in Node).
async function hashPassword(password: string, salt?: string): Promise<string> {
  const s = salt ? hex(salt) : hex(await crypto.subtle.digest("SHA-256", enc.encode(randomToken())));
  const keyMaterial = await crypto.subtle.importKey("raw", enc.encode(password), { name: "PBKDF2" }, false, ["deriveKey"]);
  const derived = await crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: enc.encode(s), iterations: 250_000, hash: "SHA-256" },
    keyMaterial, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", derived));
  return `pbkdf2-sha256$250000$${s}$${hex(raw)}`;
}
async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const m = /^\$([a-z0-9]+)\$(\d+)\$([0-9a-f]+)\$([0-9a-f]+)$/.exec(stored);
  if (!m) return false;
  const salt = m[3];
  const expected = await hashPassword(password, salt);
  return safeEqual(expected, stored);
}

// ------------------------------------------------------------------ small utils
const normUsername = (v: unknown): string | null => {
  const u = String(v ?? "").trim();
  return USERNAME_RX.test(u) ? u : null;
};
const normPassword = (v: unknown): string | null => {
  const p = String(v ?? "");
  return p.length >= PASSWORD_MIN ? p : null;
};
const authEnabled = (env: Env) => !!(env.DB && env.AUTH_SECRET);

/** fixed-window counter in D1 (the edge cache is per data centre, so it cannot rate-limit logins reliably) */
async function hit(env: Env, key: string, windowS: number): Promise<number> {
  const now = nowS();
  const r = await env.DB!.prepare(
    `INSERT INTO rate(k, n, reset) VALUES(?1, 1, ?2)
     ON CONFLICT(k) DO UPDATE SET n = CASE WHEN rate.reset <= ?3 THEN 1 ELSE rate.n + 1 END,
                                  reset = CASE WHEN rate.reset <= ?3 THEN ?2 ELSE rate.reset END
     RETURNING n`).bind(key, now + windowS, now).first<{ n: number }>();
  return r?.n ?? 1;
}

// ------------------------------------------------------------------ sessions
export async function userFromRequest(env: Env, request: Request): Promise<{ id: string; username: string; tokenHash: string; isAdmin: boolean } | null> {
  if (!env.DB) return null;
  const m = /^Bearer\s+([A-Za-z0-9_-]{30,80})$/.exec(request.headers.get("Authorization") || "");
  if (!m) return null;
  const tokenHash = await sha256Hex(m[1]);
  const row = await env.DB.prepare(
    `SELECT u.id AS id, u.username AS username, u.is_admin AS is_admin FROM sessions s JOIN users u ON u.id = s.user_id
     WHERE s.token_hash = ?1 AND s.expires > ?2`).bind(tokenHash, nowS()).first<{ id: string; username: string; is_admin: number }>();
  return row ? { id: row.id, username: row.username, tokenHash, isAdmin: !!row.is_admin } : null;
}

async function createSession(env: Env, userId: string, ua: string): Promise<{ token: string; expires: number }> {
  const token = randomToken();
  const now = nowS();
  const expires = now + SESSION_TTL_S;
  await env.DB!.batch([
    env.DB!.prepare(`INSERT INTO sessions(token_hash, user_id, created, expires, ua) VALUES(?1, ?2, ?3, ?4, ?5)`)
      .bind(await sha256Hex(token), userId, now, expires, ua.slice(0, 160)),
    env.DB!.prepare(`DELETE FROM sessions WHERE user_id = ?1 AND token_hash NOT IN
                     (SELECT token_hash FROM sessions WHERE user_id = ?1 ORDER BY created DESC LIMIT ?2)`).bind(userId, MAX_SESSIONS),
  ]);
  return { token, expires };
}

// ------------------------------------------------------------------ the routes
export function isAuthPath(p: string): boolean { return p.startsWith("/api/auth/") || p === "/api/sync"; }

export async function handleAuth(
  env: Env, request: Request, path: string, body: any, out: Out,
): Promise<Response> {
  // config answers GET and POST and says exactly what is still missing (names only, never values)
  if (path === "/api/auth/config") {
    const missing: string[] = [];
    if (!env.DB) missing.push("DB");
    if (!env.AUTH_SECRET) missing.push("AUTH_SECRET");
    return out({ enabled: missing.length === 0, missing });
  }
  if (request.method !== "POST") return out({ error: "POST only" }, 405);
  if (!authEnabled(env)) return out({ error: "Login is not configured on this server" }, 503);
  const db = env.DB!;
  const ip = request.headers.get("CF-Connecting-IP") || "anon";

  // ---- register
  if (path === "/api/auth/register") {
    const username = normUsername(body?.username);
    const password = normPassword(body?.password);
    if (!username) return out({ error: "Username must be 3-32 letters, numbers or underscores" }, 400);
    if (!password) return out({ error: `Password must be at least ${PASSWORD_MIN} characters` }, 400);
    if ((await hit(env, `ip-reg:${ip}`, 3600)) > 10) return out({ error: "Too many registrations, try again later" }, 429);
    if ((await hit(env, `un-reg:${username}`, 3600)) > 3) return out({ error: "Too many attempts, try again later" }, 429);
    const existing = await db.prepare(`SELECT 1 FROM users WHERE username = ?1`).bind(username).first();
    if (existing) return out({ error: "That username is taken" }, 409);
    const id = crypto.randomUUID();
    const passwordHash = await hashPassword(password);
    await db.prepare(`INSERT INTO users(id, username, password_hash, created, last_login) VALUES(?1, ?2, ?3, ?4, ?5)`)
      .bind(id, username, passwordHash, nowS(), nowS()).run();
    const s = await createSession(env, id, request.headers.get("User-Agent") || "");
    return out({ token: s.token, expires: s.expires, user: { id, username } });
  }

  // ---- login
  if (path === "/api/auth/login") {
    const username = normUsername(body?.username);
    const password = String(body?.password ?? "");
    if (!username || !password) return out({ error: "Enter your username and password" }, 400);
    if ((await hit(env, `ip-log:${ip}`, 600)) > 30) return out({ error: "Too many attempts, try again later" }, 429);
    if ((await hit(env, `un-log:${username}`, 600)) > 10) return out({ error: "Too many attempts, try again later" }, 429);
    const row = await db.prepare(`SELECT id, username, password_hash, is_admin FROM users WHERE username = ?1`)
      .bind(username).first<{ id: string; username: string; password_hash: string; is_admin: number }>();
    if (!row || !(await verifyPassword(password, row.password_hash))) {
      return out({ error: "Wrong username or password" }, 401);
    }
    await db.prepare(`UPDATE users SET last_login = ?2 WHERE id = ?1`).bind(row.id, nowS()).run();
    const s = await createSession(env, row.id, request.headers.get("User-Agent") || "");
    return out({ token: s.token, expires: s.expires, user: { id: row.id, username: row.username, isAdmin: !!row.is_admin } });
  }

  // ---- everything below needs a session
  const user = await userFromRequest(env, request);
  if (!user) return out({ error: "Not signed in" }, 401);

  if (path === "/api/auth/me") return out({ user: { id: user.id, username: user.username, isAdmin: user.isAdmin } });

  if (path === "/api/auth/logout") {
    if (body?.all) await db.prepare(`DELETE FROM sessions WHERE user_id = ?1`).bind(user.id).run();
    else await db.prepare(`DELETE FROM sessions WHERE token_hash = ?1`).bind(user.tokenHash).run();
    return out({ ok: true });
  }

  if (path === "/api/auth/delete") {
    if (normUsername(body?.confirm) !== user.username) return out({ error: "Type your username to confirm" }, 400);
    await db.batch([
      db.prepare(`DELETE FROM user_items WHERE user_id = ?1`).bind(user.id),
      db.prepare(`DELETE FROM sessions WHERE user_id = ?1`).bind(user.id),
      db.prepare(`DELETE FROM users WHERE id = ?1`).bind(user.id),
    ]);
    return out({ ok: true });
  }

  // ---- sync: push local changes (last writer wins per item, by the item's own timestamp), pull everything newer
  if (path === "/api/sync") {
    if ((await hit(env, `sync:${user.id}`, 60)) > 120) return out({ error: "Slow down" }, 429);
    const kind = String(body?.kind || "fav");
    if (!KINDS.has(kind)) return out({ error: "unknown kind" }, 400);
    const cursor = Math.max(0, Math.floor(Number(body?.cursor) || 0));
    const raw: any[] = Array.isArray(body?.changes) ? body.changes.slice(0, MAX_CHANGES_PER_CALL) : [];
    const horizon = Date.now() + 5 * 60_000;                       // a wrong device clock cannot win forever

    if (raw.length) {
      const live = await db.prepare(`SELECT COUNT(*) AS n FROM user_items WHERE user_id = ?1 AND kind = ?2 AND deleted = 0`)
        .bind(user.id, kind).first<{ n: number }>();
      let room = MAX_ITEMS - (live?.n ?? 0);
      const base = Date.now() * 1000;                              // + index keeps `updated` unique inside one call
      const stmts: D1PreparedStatement[] = [];
      raw.forEach((c, i) => {
        const id = typeof c?.id === "string" && c.id.length > 0 && c.id.length <= 400 ? c.id : null;
        const ts = Math.min(Math.floor(Number(c?.ts) || 0), horizon);
        if (!id || ts <= 0) return;
        const deleted = c.data === null || c.data === undefined ? 1 : 0;
        let data = "";
        if (!deleted) {
          try { data = JSON.stringify(c.data); } catch { return; }
          if (data.length > MAX_DATA_BYTES) return;
          if (room <= 0) return;
          room--;
        }
        stmts.push(db.prepare(
          `INSERT INTO user_items(user_id, kind, id, data, ts, deleted, updated) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7)
           ON CONFLICT(user_id, kind, id) DO UPDATE SET data = excluded.data, ts = excluded.ts, deleted = excluded.deleted, updated = excluded.updated
           WHERE excluded.ts > user_items.ts`).bind(user.id, kind, id, data, ts, deleted, base + i));
      });
      if (stmts.length) await db.batch(stmts);
    }

    const rows = await db.prepare(
      `SELECT id, data, ts, deleted, updated FROM user_items WHERE user_id = ?1 AND kind = ?2 AND updated >= ?3
       ORDER BY updated LIMIT ?4`).bind(user.id, kind, cursor, PULL_LIMIT + 1).all<{ id: string; data: string; ts: number; deleted: number; updated: number }>();
    const list = rows.results || [];
    const more = list.length > PULL_LIMIT;
    const page = more ? list.slice(0, PULL_LIMIT) : list;
    const changes = page.map((r) => ({ id: r.id, ts: r.ts, data: r.deleted ? null : safeParse(r.data) }));
    // ">=" on the cursor re-sends the last row once: harmless (the client applies by timestamp) and it can never skip a row
    const next = page.length ? page[page.length - 1].updated : cursor;
    return out({ changes, cursor: next, more });
  }

  return out({ error: "Not found" }, 404);
}

const safeParse = (s: string) => { try { return JSON.parse(s); } catch { return null; } };

/** called from the cron: drop expired sessions / counters and old tombstones */
export async function authMaintenance(env: Env): Promise<void> {
  if (!env.DB) return;
  const now = nowS();
  try {
    await env.DB.batch([
      env.DB.prepare(`DELETE FROM sessions WHERE expires < ?1`).bind(now),
      env.DB.prepare(`DELETE FROM rate WHERE reset < ?1`).bind(now),
      env.DB.prepare(`DELETE FROM user_items WHERE deleted = 1 AND updated < ?1`).bind((Date.now() - TOMBSTONE_KEEP_S * 1000) * 1000),
    ]);
  } catch { /* best effort */ }
}
