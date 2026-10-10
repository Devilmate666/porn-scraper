// ---------------------------------------------------------------------------------------------------------
// "One account per person" as far as a website can enforce it: limit registrations per network address.
// Only a keyed hash of the IP is stored (not the IP), and old rows are deleted after the window.
//   SIGNUP_MAX_PER_IP    accounts one network may create per window (default 1, 0 = no limit)
//   SIGNUP_WINDOW_DAYS   the window (default 30)
// The admin username (ADMIN_USERNAME) is exempt, so you can always register it.
// Honest limits: a person on another network (mobile data, VPN) can still register again, and people who share
// a network (family, school) share the limit. Raise SIGNUP_MAX_PER_IP if real users get blocked.
// ---------------------------------------------------------------------------------------------------------
import { Env } from "./types";

const enc = new TextEncoder();
const nowS = () => Math.floor(Date.now() / 1000);

async function ipKey(env: Env, request: Request): Promise<string> {
  const ip = request.headers.get("CF-Connecting-IP") || "anon";
  const key = await crypto.subtle.importKey("raw", enc.encode("signup|" + (env.AUTH_SECRET || "")), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(ip)));
  return [...mac].slice(0, 16).map((x) => x.toString(16).padStart(2, "0")).join("");
}
const maxPer = (env: Env) => { const n = parseInt(env.SIGNUP_MAX_PER_IP ?? "1", 10); return Number.isFinite(n) ? Math.max(0, n) : 1; };
const windowS = (env: Env) => Math.max(1, parseInt(env.SIGNUP_WINDOW_DAYS ?? "30", 10) || 30) * 86400;
const isAdminName = (env: Env, body: any) =>
  !!env.ADMIN_USERNAME && String(body?.username ?? "").trim().toLowerCase() === env.ADMIN_USERNAME.trim().toLowerCase();

/** text to show the visitor when registration must be refused, else null */
export async function signupBlocked(env: Env, request: Request, body: any): Promise<string | null> {
  const max = maxPer(env);
  if (!env.DB || !max || isAdminName(env, body)) return null;
  try {
    const r = await env.DB.prepare(`SELECT COUNT(*) AS n FROM signups WHERE ip_hash = ?1 AND ts > ?2`)
      .bind(await ipKey(env, request), nowS() - windowS(env)).first<{ n: number }>();
    if ((r?.n ?? 0) >= max) return "An account was already created from this network. Please sign in to it instead.";
  } catch { /* table missing (migration 0005 not run yet): do not block anybody */ }
  return null;
}

export async function signupRecord(env: Env, request: Request, body: any): Promise<void> {
  if (!env.DB || !maxPer(env) || isAdminName(env, body)) return;
  try {
    await env.DB.prepare(`INSERT INTO signups(ip_hash, ts) VALUES(?1, ?2)`).bind(await ipKey(env, request), nowS()).run();
  } catch { /* optional */ }
}

export async function signupMaintenance(env: Env): Promise<void> {
  if (!env.DB) return;
  try { await env.DB.prepare(`DELETE FROM signups WHERE ts < ?1`).bind(nowS() - windowS(env) - 86400).run(); } catch { /* optional */ }
}
