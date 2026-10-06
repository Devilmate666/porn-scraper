// ---------------------------------------------------------------------------
// Live cams, fetched by the Worker itself (port of the DIRECT platform feeds in livecams.py).
// This gives cams a SECOND refresher that does not depend on GitHub: the Worker's cron trigger and its
// stale-while-revalidate path both use it. Each platform is independent - one blocking us never empties the page.
// ---------------------------------------------------------------------------
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
export const CAMS_HOME = "https://www.lemoncams.com/";

const ROOM_URL: Record<string, string> = {
  chaturbate: "https://chaturbate.com/{u}/", stripchat: "https://stripchat.com/{u}",
  cam4: "https://www.cam4.com/{u}", camsoda: "https://www.camsoda.com/{u}",
};
const LABEL: Record<string, string> = { chaturbate: "Chaturbate", stripchat: "Stripchat", cam4: "Cam4", camsoda: "CamSoda", bongacams: "BongaCams", myfreecams: "MyFreeCams", livejasmin: "LiveJasmin", streamate: "Streamate", flirt4free: "Flirt4Free" };
const COUNTRIES: Record<string, string> = {
  US: "United States", GB: "United Kingdom", UK: "United Kingdom", CA: "Canada", MX: "Mexico", CO: "Colombia", BR: "Brazil", AR: "Argentina", CL: "Chile", PE: "Peru", VE: "Venezuela",
  EC: "Ecuador", UY: "Uruguay", DO: "Dominican Republic", CU: "Cuba", ES: "Spain", PT: "Portugal", FR: "France", DE: "Germany", IT: "Italy", NL: "Netherlands", BE: "Belgium", CH: "Switzerland",
  AT: "Austria", PL: "Poland", CZ: "Czechia", SK: "Slovakia", HU: "Hungary", RO: "Romania", BG: "Bulgaria", RS: "Serbia", HR: "Croatia", GR: "Greece", TR: "Turkey", UA: "Ukraine",
  RU: "Russia", BY: "Belarus", MD: "Moldova", LT: "Lithuania", LV: "Latvia", EE: "Estonia", SE: "Sweden", NO: "Norway", DK: "Denmark", FI: "Finland", IE: "Ireland", AU: "Australia",
  NZ: "New Zealand", JP: "Japan", KR: "South Korea", CN: "China", TH: "Thailand", PH: "Philippines", ID: "Indonesia", VN: "Vietnam", IN: "India", ZA: "South Africa", KZ: "Kazakhstan",
};
const NAME2CODE: Record<string, string> = Object.fromEntries(Object.entries(COUNTRIES).filter(([k]) => k !== "UK").map(([k, v]) => [v.toLowerCase(), k]));
const K_NAME = ["username", "user_name", "userName", "nickname", "nick", "displayName", "display_name", "model", "modelName", "name", "slug"];
const K_PROVIDER = ["provider", "providerName", "provider_name", "site", "siteName", "source", "network", "platform"];
const K_VIEWERS = ["num_users", "viewersCount", "viewers", "viewerCount", "viewer_count", "users", "numUsers", "num_viewers", "connections", "online"];
const K_THUMB = ["previewUrlThumbBig", "image_url_360x270", "img", "thumbnail", "thumbnailUrl", "thumbnail_url", "snapshotUrl", "previewUrl", "previewImage", "image_url", "thumb", "thumbUrl", "image", "imageUrl", "preview", "snapshot", "screenshot", "poster", "picture", "photo", "profileImage", "previewUrlThumbSmall", "avatarUrl"];
const K_COUNTRY = ["country", "countryName", "country_name", "countryCode", "country_code", "nation"];
const K_TITLE = ["room_subject", "subject", "topic", "title", "roomTitle", "room_title", "headline", "statusMessage", "subject_html"];
const K_LINK = ["link", "url", "profileUrl", "profile_url", "href", "permalink"];
const NOT_PUBLIC = new Set(["private", "hidden", "group", "groupshow", "group_show", "offline", "away", "ticketshow", "spy", "p2p", "password"]);

const empty = (v: any) => v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length) || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length);
const first = (d: any, keys: string[]) => { for (const k of keys) if (k in d && !empty(d[k])) return d[k]; return null; };
const addThumb = (lst: string[], u: string | null) => {
  if (!u || u.startsWith("data:")) return;
  if (u.startsWith("http://")) u = "https://" + u.slice(7);
  if (!lst.includes(u)) lst.push(u);
};
const defaultThumbs = (prov: string | null, name: string): string[] => {
  const n = encodeURIComponent(name);
  if (prov === "chaturbate" && name) return [`https://thumb.live.mmcdn.com/riw/${n}.jpg`, `https://roomimg.stream.highwebmedia.com/ri/${n}.jpg`, `https://thumb.live.mmcdn.com/ri/${n}.jpg`];
  if (prov === "cam4" && name) return [`https://snapshots.xcdnpro.com/thumbnails/${n}`];
  return [];
};
const asUrl = (v: any, base: string): string | null => {
  if (v && typeof v === "object" && !Array.isArray(v)) v = first(v, ["url", "src", "href", "large", "medium", "small", "default"]);
  if (Array.isArray(v) && v.length) return asUrl(v[0], base);
  if (typeof v === "string" && v.trim() && (/^(https?:\/\/|\/\/|\/)/.test(v.trim()) || v.includes("."))) {
    v = v.trim();
    try { return v.startsWith("//") ? "https:" + v : new URL(v, base).toString(); } catch { return null; }
  }
  return null;
};
const decode = (s: string) => s.replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#0?39;/g, "'").replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n));
const clean = (s: any, limit = 200) => decode(String(s ?? "").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim().slice(0, limit);
const flag = (code?: string | null) => { code = (code || "").toUpperCase(); return /^[A-Z]{2}$/.test(code) ? String.fromCodePoint(...[...code].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65)) : null; };
const country = (v: any): [string | null, string | null] => {
  if (v && typeof v === "object") v = first(v, ["name", "code"]);
  const s = String(v ?? "").trim();
  if (!s || ["undefined", "none", "null", "unknown"].includes(s.toLowerCase())) return [null, null];
  if (/^[A-Za-z]{2}$/.test(s)) { const c = s.toUpperCase(); return [COUNTRIES[c] || c, c === "UK" ? "GB" : c]; }
  return [s.slice(0, 40), NAME2CODE[s.toLowerCase()] || null];
};
const csv = (v: string, limit = 12) => (v || "").split(/[,;]/).map((x) => x.trim()).filter((x) => x && !x.includes("=")).slice(0, limit);
const nameList = (v: any, limit = 14): string[] => {
  if (typeof v === "string") return csv(v, limit);
  const out: string[] = [];
  for (const x of Array.isArray(v) ? v : []) {
    const s = String((x && typeof x === "object" ? x.name || x.slug : x) ?? "").trim().replace(/^#/, "");
    if (s && !out.includes(s)) out.push(s);
  }
  return out.slice(0, limit);
};
const toInt = (v: any): number | null => {
  if (typeof v === "boolean") return null;
  if (typeof v === "number") return Math.trunc(v);
  const m = String(v ?? "").match(/\d[\d.,]*/);
  return m ? parseInt(m[0].replace(/[.,]/g, ""), 10) || 0 : null;
};

function camFromDict(d: any, base: string, provider: string | null, needThumb = true): any | null {
  const rawName = first(d, K_NAME);
  if (typeof rawName !== "string" || !rawName.trim()) return null;
  const name = rawName.trim();
  const thumbs: string[] = [];
  for (const k of K_THUMB) if (k in d) addThumb(thumbs, asUrl(d[k], base));
  if (provider === "stripchat" && d.id != null && d.snapshotTimestamp) addThumb(thumbs, `https://img.doppiocdn.com/thumbs/${d.snapshotTimestamp}/${d.id}`);
  for (const u of defaultThumbs(provider, name)) addThumb(thumbs, u);
  const thumb = thumbs[0] || null;
  if (!thumb && needThumb) return null;
  const show = String(first(d, ["current_show", "showType", "status"]) ?? "").trim().toLowerCase();
  if (NOT_PUBLIC.has(show)) return null;

  let prov = provider;
  if (!prov) { let p = first(d, K_PROVIDER); if (p && typeof p === "object") p = first(p, ["name", "slug", "id"]); prov = p ? String(p).trim().toLowerCase() : null; }
  let link: string | null = prov && ROOM_URL[prov] ? ROOM_URL[prov].replace("{u}", encodeURIComponent(name)) : null;
  if (!link) link = asUrl(first(d, K_LINK), base);
  if (!link && prov) { try { link = new URL(`/${prov}/${name}`, base).toString(); } catch { /* skip */ } }

  let [ctry, code] = country(first(d, K_COUNTRY));
  let loc = typeof d.location === "string" ? clean(d.location, 60) : "";
  if (!ctry && loc) { [ctry, code] = country(loc); loc = ""; }
  const age = toInt(d.display_age || d.age);
  const langs = d.spoken_languages || d.languages || d.language;
  const g = String(d.gender || d.broadcastGender || "").trim().toLowerCase() || null;
  return {
    username: name, age: age && age >= 18 && age < 100 ? age : null, provider: prov,
    provider_name: LABEL[prov || ""] || ((prov || "").replace(/^./, (c) => c.toUpperCase()) || null), provider_logo: null,
    link, thumbnail: thumb, thumbnails: thumbs.slice(0, 5), viewers: toInt(first(d, K_VIEWERS)),
    country: ctry, country_code: code, flag: null, flag_emoji: flag(code), location: loc || null,
    gender: ({ f: "female", m: "male", t: "trans", c: "couple" } as Record<string, string>)[g || ""] || g,
    languages: langs ? nameList(langs, 6) : [], room_title: clean(first(d, K_TITLE), 160) || null,
    categories: nameList(d.categories || d.category || []), tags: nameList(d.tags || d.tag_list || []),
    hd: !!(d.is_hd || d.isHd || d.hd) || null, is_new: !!(d.is_new || d.isNew) || null,
    id: d.id != null ? String(d.id) : null, online: true,
  };
}

function roomList(data: any): any[] {
  let best: any[] = [];
  const walk = (n: any, depth = 0) => {
    if (depth > 6) return;
    if (Array.isArray(n)) {
      const ds = n.filter((x) => x && typeof x === "object" && !Array.isArray(x));
      if (ds.length >= 2 && ds.filter((x) => first(x, K_NAME)).length >= ds.length * 0.6 && ds.length > best.length) best = ds;
      for (const v of n.slice(0, 5)) walk(v, depth + 1);
    } else if (n && typeof n === "object") for (const v of Object.values(n)) walk(v, depth + 1);
  };
  walk(data);
  return best;
}

const DIRECT: Record<string, { base: string; candidates: string[][] }> = {
  chaturbate: { base: "https://chaturbate.com", candidates: [
    [0, 90, 180].map((o) => `https://chaturbate.com/api/ts/roomlist/room-list/?enable_recommendations=false&limit=90&offset=${o}`),
    [0, 100].map((o) => `https://chaturbate.com/api/public/affiliates/onlinerooms/?wm=dvafl&format=json&limit=100&offset=${o}`) ] },
  stripchat: { base: "https://stripchat.com", candidates: [
    [0, 60, 120].map((o) => `https://stripchat.com/api/front/v2/models?limit=60&offset=${o}&primaryTag=girls&sortBy=stripRanking`),
    [0, 60].map((o) => `https://stripchat.com/api/front/models?limit=60&offset=${o}&primaryTag=girls&sortBy=stripRanking`) ] },
  cam4: { base: "https://www.cam4.com", candidates: [[1, 2].map((p) => `https://www.cam4.com/directoryCams?directoryJson=true&online=true&url=true&page=${p}&resultsPerPage=60&gender=female`)] },
  camsoda: { base: "https://www.camsoda.com", candidates: [[1, 2].map((p) => `https://www.camsoda.com/api/v1/browse/react?p=${p}&perPage=60`)] },
};

async function getJson(url: string, headers: Record<string, string>, ms = 12000): Promise<any> {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    const r = await fetch(url, { headers, signal: ac.signal });
    if (r.status >= 400) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally { clearTimeout(t); }
}

async function fetchDirect(name: string): Promise<{ cams: any[]; notes: string[] }> {
  const spec = DIRECT[name], base = spec.base, notes: string[] = [];
  const headers = { "User-Agent": UA, Accept: "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", Referer: base + "/", Origin: base, "X-Requested-With": "XMLHttpRequest" };
  for (const cand of spec.candidates) {
    const cams: any[] = [], seen = new Set<string>();
    for (const u of cand) {
      let data: any;
      try { data = await getJson(u, headers); } catch (e) { notes.push(`${name}: ${u.split("?")[0]} -> ${e instanceof Error ? e.message : e}`); break; }
      let fresh = 0;
      for (const d of roomList(data)) {
        const cam = camFromDict(d, base, name, false);
        if (cam && !seen.has(cam.username.toLowerCase())) { seen.add(cam.username.toLowerCase()); cams.push(cam); fresh++; }
      }
      if (!fresh) { notes.push(`${name}: ${u.split("?")[0]} returned no usable rooms`); break; }
    }
    if (cams.length) { notes.push(`${name}: ${cams.length} cams`); return { cams, notes }; }
  }
  return { cams: [], notes };
}

function finish(cam: any): any {
  const prov = (cam.provider || "").toLowerCase();
  if (ROOM_URL[prov] && cam.username) {
    const direct = ROOM_URL[prov].replace("{u}", encodeURIComponent(cam.username));
    if (cam.link !== direct) { cam.lemoncams_link = cam.link; cam.link = direct; }
  }
  if (!cam.provider_name) cam.provider_name = LABEL[prov] || prov.replace(/^./, (c: string) => c.toUpperCase()) || null;
  const thumbs: string[] = [];
  for (const u of [cam.thumbnail, ...(cam.thumbnails || []), ...defaultThumbs(prov, cam.username)]) addThumb(thumbs, u);
  cam.thumbnails = thumbs.slice(0, 5);
  cam.thumbnail = thumbs[0] || null;
  for (const k of ["categories", "tags", "languages"]) cam[k] = cam[k] || [];
  for (const k of ["country_code", "flag_emoji", "gender", "location", "hd", "is_new"]) if (!(k in cam)) cam[k] = null;
  if (!cam.flag_emoji && cam.country) { cam.country_code = cam.country_code || country(cam.country)[1]; cam.flag_emoji = flag(cam.country_code); }
  return cam;
}

function merge(groups: Record<string, any[]>): any[] {
  const seen = new Set<string>(), per: Record<string, any[]> = {};
  for (const [src, cams] of Object.entries(groups)) for (let cam of cams) {
    cam = finish(cam);
    const prov = cam.provider || src, key = `${prov}|${(cam.username || "").toLowerCase()}`;
    if (!cam.link || seen.has(key)) continue;
    seen.add(key);
    (per[prov] = per[prov] || []).push(cam);
  }
  const lists = Object.values(per);
  for (const l of lists) l.sort((a, b) => (b.viewers || 0) - (a.viewers || 0));
  const out: any[] = [];
  for (let i = 0; lists.some((l) => i < l.length) && out.length < 500; i++) for (const l of lists) if (i < l.length) out.push(l[i]);
  return out;
}

/** Fetch every direct platform in parallel (each independent). Returns a payload even when some platforms fail. */
export async function fetchLiveCams(): Promise<any> {
  const names = Object.keys(DIRECT);
  const results = await Promise.all(names.map((n) => fetchDirect(n).catch((e) => ({ cams: [] as any[], notes: [`${n}: ${e instanceof Error ? e.message : e}`] }))));
  const groups: Record<string, any[]> = {}, diag: string[] = [];
  names.forEach((n, i) => { groups[n] = results[i].cams; diag.push(...results[i].notes); });
  const items = merge(groups);
  if (!items.length) return { page: CAMS_HOME, items: [], count: 0, diagnostics: diag, error: "No live cams could be loaded: none of the cam platforms answered." };
  const by: Record<string, number> = {};
  for (const c of items) { const k = c.provider_name || c.provider || "?"; by[k] = (by[k] || 0) + 1; }
  const now = Math.floor(Date.now() / 1000);
  return { page: CAMS_HOME, items, count: items.length, providers: by, source: "direct-worker", diagnostics: diag, fetched_at: now, _ts: now };
}
