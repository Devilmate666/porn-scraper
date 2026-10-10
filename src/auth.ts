// ---------------------------------------------------------------------------------------------------------
// Email login (passwordless, 6-digit code) + per-account data sync, stored in Cloudflare D1.
//
//   POST /api/auth/config    -> { enabled }                       (the page hides the account button when false)
//   POST /api/auth/request   { email }            -> { ok }       emails a 6-digit code (10 min, 5 tries)
//   POST /api/auth/verify    { email, code }      -> { token, user, expires }
//   POST /api/auth/me        (Bearer)             -> { user }
//   POST /api/auth/logout    (Bearer) { all? }    -> { ok }       all:true ends every session of the account
//   POST /api/auth/delete    (Bearer) { confirm: email } -> { ok } deletes the account and all its data
//   POST /api/sync           (Bearer) { kind, cursor, changes:[{id, ts, data|null}] } -> { changes, cursor, more }
//
// Why a code and not a password: nothing secret to leak or reset, works from any device, no password UI to build.
// Sessions are random 256-bit tokens sent as "Authorization: Bearer"; only their SHA-256 hash is stored.
// Why D1 and not KV: KV's free tier allows 1,000 writes/day for everything; D1 allows 100,000 row writes/day.
// ---------------------------------------------------------------------------------------------------------
import { Env } from "./types";

const CODE_TTL_S = 10 * 60;
const CODE_MAX_TRIES = 5;
const RESEND_GAP_S = 45;                   // one code per address every 45 s
const SESSION_TTL_S = 90 * 86400;
const MAX_SESSIONS = 10;                   // per account; the oldest are dropped
const MAX_ITEMS = 5000;                    // live (not deleted) items per account and kind
const MAX_DATA_BYTES = 8 * 1024;           // one item
const MAX_CHANGES_PER_CALL = 500;
const PULL_LIMIT = 1000;
const TOMBSTONE_KEEP_S = 90 * 86400;
const KINDS = new Set(["fav"]);            // add "history", "settings"... here when the page starts syncing them
const EMAIL_RX = /^[^\s@<>()\[\]\\,;:"]+@[^\s@<>()\[\]\\,;:"]+\.[A-Za-z]{2,}$/;

type Out = (data: unknown, status?: number, extra?: Record<string, string>) => Response;
const nowS = () => Math.floor(Date.now() / 1000);

// ------------------------------------------------------------------ crypto helpers
const enc = new TextEncoder();
const hex = (b: ArrayBuffer | Uint8Array) => [...new Uint8Array(b as ArrayBuffer)].map((x) => x.toString(16).padStart(2, "0")).join("");
const b64url = (b: Uint8Array) => btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function sha256Hex(s: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}
async function hmacHex(secret: string, msg: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
/** uniform 6-digit code (rejection sampling, no modulo bias) */
function randomCode(): string {
  const buf = new Uint32Array(1);
  const limit = 4_294_000_000;             // largest multiple of 1e6 below 2^32
  do { crypto.getRandomValues(buf); } while (buf[0] >= limit);
  return String(buf[0] % 1_000_000).padStart(6, "0");
}
function randomToken(): string {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return b64url(b);
}

// ------------------------------------------------------------------ small utils
const normEmail = (v: unknown): string | null => {
  const e = String(v ?? "").trim().toLowerCase();
  return e.length <= 254 && EMAIL_RX.test(e) ? e : null;
};
const authEnabled = (env: Env) => !!(env.DB && env.AUTH_SECRET && (env.RESEND_API_KEY || env.BREVO_API_KEY) && env.MAIL_FROM);

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

// ------------------------------------------------------------------ email
async function sendMail(env: Env, to: string, code: string): Promise<boolean> {
  const subject = `${code} is your sign-in code`;
  const text = `Your sign-in code is ${code}\n\nIt expires in 10 minutes. If you did not ask for it, ignore this email.`;
  const html = `<div style="font-family:system-ui,sans-serif;max-width:420px;margin:auto;padding:24px">
<p style="color:#444">Your sign-in code:</p>
<p style="font-size:34px;font-weight:700;letter-spacing:8px;margin:12px 0">${code}</p>
<p style="color:#666;font-size:13px">It expires in 10 minutes. If you did not ask for it, you can ignore this email.</p></div>`;
  try {
    if (env.RESEND_API_KEY) {
      const r = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({ from: env.MAIL_FROM, to: [to], subject, text, html }),
      });
      if (!r.ok) console.log("resend", r.status, (await r.text()).slice(0, 200));
      return r.ok;
    }
    if (env.BREVO_API_KEY) {
      const m = /^\s*(?:"?([^"<]*)"?\s*)?<([^>]+)>\s*$/.exec(env.MAIL_FROM || "");
      const sender = m ? { name: (m[1] || "").trim() || undefined, email: m[2].trim() } : { email: (env.MAIL_FROM || "").trim() };
      const r = await fetch("https://api.brevo.com/v3/smtp/email", {
        method: "POST",
        headers: { "api-key": env.BREVO_API_KEY, "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ sender, to: [{ email: to }], subject, textContent: text, htmlContent: html }),
      });
      if (!r.ok) console.log("brevo", r.status, (await r.text()).slice(0, 200));
      return r.ok;
    }
  } catch (e) { console.log("mail error", String(e).slice(0, 200)); }
  return false;
}

// ------------------------------------------------------------------ sessions
export async function userFromRequest(env: Env, request: Request): Promise<{ id: string; email: string; tokenHash: string } | null> {
  if (!env.DB) return null;
  const m = /^Bearer\s+([A-Za-z0-9_-]{30,80})$/.exec(request.headers.get("Authorization") || "");
  if (!m) return null;
  const tokenHash = await sha256Hex(m[1]);
  const row = await env.DB.prepare(
    `SELECT u.id AS id, u.email AS email FROM sessions s JOIN users u ON u.id = s.user_id
     WHERE s.token_hash = ?1 AND s.expires > ?2`).bind(tokenHash, nowS()).first<{ id: string; email: string }>();
  return row ? { ...row, tokenHash } : null;
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
  if (request.method !== "POST") return out({ error: "POST only" }, 405);
  if (path === "/api/auth/config") return out({ enabled: authEnabled(env) });
  if (!authEnabled(env)) return out({ error: "Login is not configured on this server" }, 503);
  const db = env.DB!;
  const ip = request.headers.get("CF-Connecting-IP") || "anon";

  // ---- ask for a code
  if (path === "/api/auth/request") {
    const email = normEmail(body?.email);
    if (!email) return out({ error: "Enter a valid email address" }, 400);
    if ((await hit(env, `ip-req:${ip}`, 3600)) > 12) return out({ error: "Too many requests, try again later" }, 429);
    if ((await hit(env, `em-req:${email}`, 3600)) > 6) return out({ error: "Too many codes for this address, try again in an hour" }, 429);
    const prev = await db.prepare(`SELECT sent FROM login_codes WHERE email = ?1`).bind(email).first<{ sent: number }>();
    if (prev && nowS() - prev.sent < RESEND_GAP_S) {
      return out({ ok: true, retry_after: RESEND_GAP_S - (nowS() - prev.sent) });    // looks like success; the earlier code is still valid
    }
    const code = randomCode();
    const codeHash = await hmacHex(env.AUTH_SECRET!, `${email}|${code}`);
    await db.prepare(
      `INSERT INTO login_codes(email, code_hash, expires, attempts, sent) VALUES(?1, ?2, ?3, 0, ?4)
       ON CONFLICT(email) DO UPDATE SET code_hash = ?2, expires = ?3, attempts = 0, sent = ?4`)
      .bind(email, codeHash, nowS() + CODE_TTL_S, nowS()).run();
    if (!(await sendMail(env, email, code))) {
      await db.prepare(`DELETE FROM login_codes WHERE email = ?1`).bind(email).run();
      return out({ error: "Could not send the email, try again in a minute" }, 502);
    }
    return out({ ok: true, retry_after: RESEND_GAP_S });
  }

  // ---- check the code, open a session
  if (path === "/api/auth/verify") {
    const email = normEmail(body?.email);
    const code = String(body?.code ?? "").replace(/\s+/g, "");
    if (!email || !/^\d{6}$/.test(code)) return out({ error: "Enter the 6-digit code" }, 400);
    if ((await hit(env, `ip-ver:${ip}`, 600)) > 40) return out({ error: "Too many attempts, try again later" }, 429);
    // count the try first (atomic), then compare: parallel guesses cannot beat the limit
    const row = await db.prepare(`UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?1 RETURNING code_hash, expires, attempts`)
      .bind(email).first<{ code_hash: string; expires: number; attempts: number }>();
    if (!row || row.expires < nowS()) return out({ error: "That code expired. Ask for a new one." }, 400);
    if (row.attempts > CODE_MAX_TRIES) return out({ error: "Too many wrong codes. Ask for a new one." }, 429);
    const given = await hmacHex(env.AUTH_SECRET!, `${email}|${code}`);
    if (!safeEqual(given, row.code_hash)) return out({ error: `Wrong code (${Math.max(0, CODE_MAX_TRIES - row.attempts)} tries left)` }, 400);

    await db.prepare(`DELETE FROM login_codes WHERE email = ?1`).bind(email).run();      // single use
    let user = await db.prepare(`SELECT id, email FROM users WHERE email = ?1`).bind(email).first<{ id: string; email: string }>();
    if (!user) {
      const id = crypto.randomUUID();
      await db.prepare(`INSERT OR IGNORE INTO users(id, email, created, last_login) VALUES(?1, ?2, ?3, ?3)`).bind(id, email, nowS()).run();
      user = await db.prepare(`SELECT id, email FROM users WHERE email = ?1`).bind(email).first<{ id: string; email: string }>();
    } else {
      await db.prepare(`UPDATE users SET last_login = ?2 WHERE id = ?1`).bind(user.id, nowS()).run();
    }
    if (!user) return out({ error: "Could not create the account" }, 500);
    const s = await createSession(env, user.id, request.headers.get("User-Agent") || "");
    return out({ token: s.token, expires: s.expires, user });
  }

  // ---- everything below needs a session
  const user = await userFromRequest(env, request);
  if (!user) return out({ error: "Not signed in" }, 401);

  if (path === "/api/auth/me") return out({ user: { id: user.id, email: user.email } });

  if (path === "/api/auth/logout") {
    if (body?.all) await db.prepare(`DELETE FROM sessions WHERE user_id = ?1`).bind(user.id).run();
    else await db.prepare(`DELETE FROM sessions WHERE token_hash = ?1`).bind(user.tokenHash).run();
    return out({ ok: true });
  }

  if (path === "/api/auth/delete") {
    if (normEmail(body?.confirm) !== user.email) return out({ error: "Type your email address to confirm" }, 400);
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

/** called from the cron: drop expired codes / sessions / counters and old tombstones */
export async function authMaintenance(env: Env): Promise<void> {
  if (!env.DB) return;
  const now = nowS();
  try {
    await env.DB.batch([
      env.DB.prepare(`DELETE FROM login_codes WHERE expires < ?1`).bind(now - 3600),
      env.DB.prepare(`DELETE FROM sessions WHERE expires < ?1`).bind(now),
      env.DB.prepare(`DELETE FROM rate WHERE reset < ?1`).bind(now),
      env.DB.prepare(`DELETE FROM user_items WHERE deleted = 1 AND updated < ?1`).bind((Date.now() - TOMBSTONE_KEEP_S * 1000) * 1000),
    ]);
  } catch { /* best effort */ }
}
