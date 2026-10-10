// ---------------------------------------------------------------------------------------------------------
// Admin API (D1). Only the admin account can call it. Every endpoint is POST + "Authorization: Bearer <session>".
//
// Who is an admin: the account whose username equals the ADMIN_USERNAME variable (wrangler.toml, default none),
// or any account with users.is_admin = 1 (set by an admin from the panel).
//
//   /api/admin/me           -> { admin: true, username }
//   /api/admin/stats        -> counters
//   /api/admin/users        { q, sort, dir, limit, offset }   -> { users[], total }
//   /api/admin/user         { id, offset, limit }             -> { user, sessions[], items[], counts }
//   /api/admin/revoke       { id }                            sign the user out everywhere
//   /api/admin/delete-user  { id, confirm: username }         delete account + sessions + synced data
//   /api/admin/set-admin    { id, value }                     grant / remove admin
//   /api/admin/clear-rate                                     reset the login rate-limit counters
//   /api/admin/log                                            last 100 admin actions
//
// Passwords are stored only as salted hashes; nobody (including the admin) can read them, and no endpoint returns them.
// ---------------------------------------------------------------------------------------------------------
import { Env } from "./types";
import * as authMod from "./auth";

type Out = (data: unknown, status?: number, extra?: Record<string, string>) => Response;
const nowS = () => Math.floor(Date.now() / 1000);
const enc = new TextEncoder();
const hex = (b: ArrayBuffer) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const sha256Hex = async (s: string) => hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
async function hmacHex(secret: string, msg: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}

export const isAdminPath = (p: string) => p.startsWith("/api/admin/");

const SORTS: Record<string, string> = {
  created: "u.created", last_login: "u.last_login", username: "u.username COLLATE NOCASE",
  favorites: "favorites", sessions: "sessions",
};

/** the account behind the Bearer token, or null */
async function sessionUserId(env: Env, request: Request): Promise<string | null> {
  try {                                            // preferred: ask the auth module itself (it knows its own token format)
    const f = (authMod as any).userFromRequest;
    if (typeof f === "function") {
      const u = await f(env, request);
      if (u && u.id) return String(u.id);
    }
  } catch { /* fall through to the direct lookup */ }
  const m = /^Bearer\s+([A-Za-z0-9._~+\/=-]{20,300})$/.exec(request.headers.get("Authorization") || "");
  if (!m || !env.DB) return null;
  const a = await sha256Hex(m[1]);
  const b = env.AUTH_SECRET ? await hmacHex(env.AUTH_SECRET, m[1]) : a;
  const row = await env.DB.prepare(`SELECT user_id FROM sessions WHERE token_hash IN (?1, ?2) AND expires > ?3`)
    .bind(a, b, nowS()).first<{ user_id: string }>();
  return row ? row.user_id : null;
}

async function hit(env: Env, key: string, windowS: number): Promise<number> {
  const now = nowS();
  const r = await env.DB!.prepare(
    `INSERT INTO rate(k, n, reset) VALUES(?1, 1, ?2)
     ON CONFLICT(k) DO UPDATE SET n = CASE WHEN rate.reset <= ?3 THEN 1 ELSE rate.n + 1 END,
                                  reset = CASE WHEN rate.reset <= ?3 THEN ?2 ELSE rate.reset END
     RETURNING n`).bind(key, now + windowS, now).first<{ n: number }>();
  return r?.n ?? 1;
}

const isReserved = (env: Env, username: string) =>
  !!env.ADMIN_USERNAME && username.toLowerCase() === env.ADMIN_USERNAME.trim().toLowerCase();

async function log(env: Env, admin: string, action: string, target: string, ip: string) {
  try {
    await env.DB!.prepare(`INSERT INTO admin_log(ts, admin, action, target, ip) VALUES(?1, ?2, ?3, ?4, ?5)`)
      .bind(nowS(), admin, action, target.slice(0, 200), ip).run();
  } catch { /* the log table is optional (migration 0004) */ }
}
const safeJson = (s: string) => { try { return JSON.parse(s); } catch { return null; } };
const int = (v: unknown, d: number, min: number, max: number) => Math.min(max, Math.max(min, Math.floor(Number(v)) || d));

export async function handleAdmin(env: Env, request: Request, path: string, body: any, out: Out): Promise<Response> {
  if (request.method !== "POST") return out({ error: "POST only" }, 405);
  if (!env.DB) return out({ error: "Database not configured" }, 503);
  const db = env.DB;
  const ip = request.headers.get("CF-Connecting-IP") || "anon";

  if ((await hit(env, `admin:${ip}`, 600)) > 600) return out({ error: "Too many requests" }, 429);
  const uid = await sessionUserId(env, request);
  if (!uid) return out({ error: "Not signed in" }, 401);

  let me: { id: string; username: string; is_admin: number } | null = null;
  try {
    me = await db.prepare(`SELECT id, username, is_admin FROM users WHERE id = ?1`).bind(uid).first<{ id: string; username: string; is_admin: number }>();
  } catch {
    const r = await db.prepare(`SELECT id, username FROM users WHERE id = ?1`).bind(uid).first<{ id: string; username: string }>();
    me = r ? { ...r, is_admin: 0 } : null;
  }
  if (!me || !(me.is_admin === 1 || isReserved(env, me.username))) return out({ error: "Not an admin" }, 403);

  // ------------------------------------------------------------------ read-only
  if (path === "/api/admin/me") return out({ admin: true, username: me.username, id: me.id });

  if (path === "/api/admin/stats") {
    const t = nowS();
    const one = async (sql: string, ...a: unknown[]) => ((await db.prepare(sql).bind(...a).first<{ n: number }>())?.n) ?? 0;
    return out({
      users: await one(`SELECT COUNT(*) n FROM users`),
      admins: await one(`SELECT COUNT(*) n FROM users WHERE is_admin = 1 OR (?1 <> '' AND lower(username) = ?1)`, (env.ADMIN_USERNAME || "").trim().toLowerCase()),
      signups_24h: await one(`SELECT COUNT(*) n FROM users WHERE created > ?1`, t - 86400),
      signups_7d: await one(`SELECT COUNT(*) n FROM users WHERE created > ?1`, t - 7 * 86400),
      active_24h: await one(`SELECT COUNT(*) n FROM users WHERE last_login > ?1`, t - 86400),
      active_7d: await one(`SELECT COUNT(*) n FROM users WHERE last_login > ?1`, t - 7 * 86400),
      sessions: await one(`SELECT COUNT(*) n FROM sessions WHERE expires > ?1`, t),
      favorites: await one(`SELECT COUNT(*) n FROM user_items WHERE kind = 'fav' AND deleted = 0`),
      removed: await one(`SELECT COUNT(*) n FROM user_items WHERE deleted = 1`),
      now: t,
    });
  }

  if (path === "/api/admin/users") {
    const q = String(body?.q ?? "").trim().slice(0, 64);
    const like = "%" + q.replace(/[\\%_]/g, (c) => "\\" + c) + "%";
    const sort = SORTS[String(body?.sort)] || SORTS.created;
    const dir = String(body?.dir).toLowerCase() === "asc" ? "ASC" : "DESC";
    const limit = int(body?.limit, 50, 1, 500);
    const offset = int(body?.offset, 0, 0, 1_000_000);
    const t = nowS();
    const total = ((await db.prepare(`SELECT COUNT(*) n FROM users u WHERE (?1 = '' OR u.username LIKE ?2 ESCAPE '\\')`)
      .bind(q, like).first<{ n: number }>())?.n) ?? 0;
    const rows = await db.prepare(
      `SELECT u.id, u.username, u.created, u.last_login, u.is_admin,
              (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id AND s.expires > ?3) AS sessions,
              (SELECT COUNT(*) FROM user_items i WHERE i.user_id = u.id AND i.kind = 'fav' AND i.deleted = 0) AS favorites
         FROM users u WHERE (?1 = '' OR u.username LIKE ?2 ESCAPE '\\')
        ORDER BY ${sort} ${dir}, u.id LIMIT ?4 OFFSET ?5`).bind(q, like, t, limit, offset).all<any>();
    const users = (rows.results || []).map((u) => ({ ...u, is_admin: u.is_admin === 1 || isReserved(env, u.username) ? 1 : 0, reserved: isReserved(env, u.username) }));
    return out({ users, total, limit, offset });
  }

  if (path === "/api/admin/user") {
    const id = String(body?.id ?? "");
    const u = await db.prepare(`SELECT id, username, created, last_login, is_admin FROM users WHERE id = ?1`).bind(id).first<any>();
    if (!u) return out({ error: "User not found" }, 404);
    const limit = int(body?.limit, 300, 1, 500);
    const offset = int(body?.offset, 0, 0, 1_000_000);
    const t = nowS();
    const sessions = (await db.prepare(`SELECT created, expires, ua FROM sessions WHERE user_id = ?1 ORDER BY created DESC LIMIT 50`)
      .bind(id).all<any>()).results.map((s) => ({ ...s, active: s.expires > t }));
    const items = (await db.prepare(
      `SELECT kind, id, data, ts, updated FROM user_items WHERE user_id = ?1 AND deleted = 0 ORDER BY ts DESC LIMIT ?2 OFFSET ?3`)
      .bind(id, limit, offset).all<any>()).results.map((r) => ({ kind: r.kind, id: r.id, ts: r.ts, data: safeJson(r.data) }));
    const c = await db.prepare(
      `SELECT SUM(CASE WHEN deleted = 0 THEN 1 ELSE 0 END) AS live, SUM(CASE WHEN deleted = 1 THEN 1 ELSE 0 END) AS removed
         FROM user_items WHERE user_id = ?1`).bind(id).first<any>();
    return out({
      user: { ...u, is_admin: u.is_admin === 1 || isReserved(env, u.username) ? 1 : 0, reserved: isReserved(env, u.username) },
      sessions, items, counts: { live: c?.live || 0, removed: c?.removed || 0 }, limit, offset,
    });
  }

  if (path === "/api/admin/log") {
    try {
      const r = await db.prepare(`SELECT ts, admin, action, target, ip FROM admin_log ORDER BY id DESC LIMIT 100`).all<any>();
      return out({ log: r.results || [] });
    } catch { return out({ log: [], note: "run migration 0004_admin.sql to enable the log" }); }
  }

  // ------------------------------------------------------------------ actions (all logged)
  if (path === "/api/admin/revoke") {
    const id = String(body?.id ?? "");
    const u = await db.prepare(`SELECT username FROM users WHERE id = ?1`).bind(id).first<{ username: string }>();
    if (!u) return out({ error: "User not found" }, 404);
    await db.prepare(`DELETE FROM sessions WHERE user_id = ?1`).bind(id).run();
    await log(env, me.username, "revoke-sessions", u.username, ip);
    return out({ ok: true });
  }

  if (path === "/api/admin/delete-user") {
    const id = String(body?.id ?? "");
    const u = await db.prepare(`SELECT id, username FROM users WHERE id = ?1`).bind(id).first<{ id: string; username: string }>();
    if (!u) return out({ error: "User not found" }, 404);
    if (u.id === me.id) return out({ error: "You cannot delete your own admin account here" }, 400);
    if (isReserved(env, u.username)) return out({ error: "The main admin account cannot be deleted" }, 400);
    if (String(body?.confirm ?? "").trim().toLowerCase() !== u.username.toLowerCase()) return out({ error: "Type the username to confirm" }, 400);
    await db.batch([
      db.prepare(`DELETE FROM user_items WHERE user_id = ?1`).bind(id),
      db.prepare(`DELETE FROM sessions WHERE user_id = ?1`).bind(id),
      db.prepare(`DELETE FROM users WHERE id = ?1`).bind(id),
    ]);
    await log(env, me.username, "delete-user", u.username, ip);
    return out({ ok: true });
  }

  if (path === "/api/admin/set-admin") {
    const id = String(body?.id ?? "");
    const value = body?.value ? 1 : 0;
    const u = await db.prepare(`SELECT id, username FROM users WHERE id = ?1`).bind(id).first<{ id: string; username: string }>();
    if (!u) return out({ error: "User not found" }, 404);
    if (u.id === me.id) return out({ error: "You cannot change your own admin status" }, 400);
    if (isReserved(env, u.username)) return out({ error: "The main admin account is always an admin" }, 400);
    await db.prepare(`UPDATE users SET is_admin = ?2 WHERE id = ?1`).bind(id, value).run();
    await log(env, me.username, value ? "grant-admin" : "remove-admin", u.username, ip);
    return out({ ok: true });
  }

  if (path === "/api/admin/clear-rate") {
    await db.prepare(`DELETE FROM rate WHERE k NOT LIKE 'admin:%'`).run();
    await log(env, me.username, "clear-rate-limits", "-", ip);
    return out({ ok: true });
  }

  return out({ error: "Not found" }, 404);
}
