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
const LEMONCAMS_URL = "https://www.lemoncams.com/";
const LEMONCAMS_PROXY = ""; // Set via fetchLiveCams opts.proxy

function proxiedUrl(url: string, proxy: string): string {
  if (proxy && url.startsWith("http")) {
    return proxy + encodeURIComponent(url);
  }
  return url;
}

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

const STRONG_NAME = ["username", "user_name", "userName", "nickname", "nick", "displayName", "display_name", "model", "modelName"];
/** a real room has a username-like key, or a generic name PLUS viewers/thumbnail: category / filter lists have neither */
const isRoom = (x: any): boolean =>
  !!x && typeof x === "object" && !Array.isArray(x) &&
  (STRONG_NAME.some((k) => typeof x[k] === "string" && x[k].trim()) || (!!first(x, ["name", "slug"]) && (first(x, K_VIEWERS) !== null || first(x, K_THUMB) !== null)));

function roomList(data: any): any[] {
  let best: any[] = [];
  const walk = (n: any, depth = 0) => {
    if (depth > 6) return;
    if (Array.isArray(n)) {
      const dicts = n.filter((x) => x && typeof x === "object" && !Array.isArray(x));
      const rooms = dicts.filter(isRoom);
      if (rooms.length >= 2 && rooms.length >= dicts.length * 0.6 && rooms.length > best.length) best = rooms;
      for (const v of n.slice(0, 5)) walk(v, depth + 1);
    } else if (n && typeof n === "object") for (const v of Object.values(n)) walk(v, depth + 1);
  };
  walk(data);
  return best;
}

type Spec = { base: string; candidates: string[][] };
const directSpecs = (wm: string): Record<string, Spec> => ({
  chaturbate: { base: "https://chaturbate.com", candidates: [
    [0, 90, 180].map((o) => `https://chaturbate.com/api/ts/roomlist/room-list/?enable_recommendations=false&limit=90&offset=${o}`),
    [0, 100].map((o) => `https://chaturbate.com/api/public/affiliates/onlinerooms/?wm=${encodeURIComponent(wm)}&format=json&limit=100&offset=${o}`) ] },
  stripchat: { base: "https://stripchat.com", candidates: [
    [0, 60, 120].map((o) => `https://stripchat.com/api/front/v2/models?limit=60&offset=${o}&primaryTag=girls&sortBy=stripRanking`),
    [0, 60].map((o) => `https://stripchat.com/api/front/models?limit=60&offset=${o}&primaryTag=girls&sortBy=stripRanking`) ] },
  cam4: { base: "https://www.cam4.com", candidates: [[1, 2].map((p) => `https://www.cam4.com/directoryCams?directoryJson=true&online=true&url=true&page=${p}&resultsPerPage=60&gender=female`)] },
  camsoda: { base: "https://www.camsoda.com", candidates: [[1, 2].map((p) => `https://www.camsoda.com/api/v1/browse/react?p=${p}&perPage=60`)] },
  lemoncams: { base: LEMONCAMS_URL, candidates: [[1, 2, 3, 4, 5].map((p) => `https://www.lemoncams.com/api/live?page=${p}&per_page=60&sort=rating`)] },
});

const LEMONCAMS_PAGES = [
  "https://www.lemoncams.com/",
  "https://www.lemoncams.com/female",
  "https://www.lemoncams.com/male",
  "https://www.lemoncams.com/couple",
  "https://www.lemoncams.com/trans",
];

async function fetchLemoncams(proxy: string = ""): Promise<{ cams: any[]; notes: string[] }> {
  const notes: string[] = [];
  const allCams: any[] = [];
  const seen = new Set<string>();

  const headers = { "User-Agent": UA, Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", Referer: LEMONCAMS_URL };

  for (const url of LEMONCAMS_PAGES) {
    try {
      const r = await fetch(proxiedUrl(url, proxy), { headers, signal: AbortSignal.timeout(15000) });
      if (!r.ok) { notes.push(`lemoncams: ${url} -> HTTP ${r.status}`); continue; }
      const html = await r.text();
      const camUrls = [...html.matchAll(/href="(\/cam\/[^"]+)"/g)].map(m => `https://www.lemoncams.com${m[1]}`).slice(0, 30);
      for (const camUrl of camUrls) {
        try {
          const cr = await fetch(proxiedUrl(camUrl, proxy), { headers, signal: AbortSignal.timeout(8000) });
          if (!cr.ok) continue;
          const cHtml = await cr.text();
          const providerMatch = cHtml.match(/data-provider=["']([^"']+)["']/i);
          const nameMatch = cHtml.match(/data-username=["']([^"']+)["']/i) || cHtml.match(/<title>([^<]+)<\/title>/i);
          const thumbMatch = cHtml.match(/data-thumb=["']([^"']+)["']/i) || cHtml.match(/<meta property="og:image" content="([^"]+)"/i);
          const viewersMatch = cHtml.match(/data-viewers=["'](\d+)["']/i);
          const countryMatch = cHtml.match(/data-country=["']([^"']+)["']/i);
          const tagsMatch = cHtml.match(/data-tags=["']([^"']+)["']/i);
          const categoriesMatch = cHtml.match(/data-categories=["']([^"']+)["']/i);
          const hdMatch = cHtml.match(/data-hd=["'](true|false)["']/i);
          const newMatch = cHtml.match(/data-new=["'](true|false)["']/i);
          const idMatch = cHtml.match(/data-id=["']([^"']+)["']/i);

          const provider = providerMatch ? providerMatch[1].toLowerCase() : null;
          const name = nameMatch ? nameMatch[1].replace(/\s*[-|]\s*lemoncams.*/i, "").trim() : null;
          if (!name) continue;

          const key = `${provider || "lemoncams"}|${name.toLowerCase()}`;
          if (seen.has(key)) continue;
          seen.add(key);

          const cam = camFromDict({
            username: name,
            provider,
            thumbnail: thumbMatch ? thumbMatch[1] : null,
            viewers: viewersMatch ? parseInt(viewersMatch[1], 10) : null,
            country: countryMatch ? countryMatch[1] : null,
            tags: tagsMatch ? tagsMatch[1].split(",").map(s => s.trim()) : [],
            categories: categoriesMatch ? categoriesMatch[1].split(",").map(s => s.trim()) : [],
            is_hd: hdMatch ? hdMatch[1] === "true" : null,
            is_new: newMatch ? newMatch[1] === "true" : null,
            id: idMatch ? idMatch[1] : null,
          }, LEMONCAMS_URL, provider);

          if (cam) allCams.push(cam);
        } catch { /* skip individual cam */ }
      }
    } catch (e) {
      notes.push(`lemoncams: ${url} -> ${e instanceof Error ? e.message : e}`);
    }
  }
  return { cams: allCams, notes };
}

async function fetchLemoncamsTags(proxy: string = ""): Promise<{ tags: string[]; categories: string[]; genres: string[]; hairColors: string[]; bodyTypes: string[]; notes: string[] }> {
  const notes: string[] = [];
  const tags = new Set<string>();
  const categories = new Set<string>();
  const genres = new Set<string>();
  const hairColors = new Set<string>();
  const bodyTypes = new Set<string>();

  const headers = { "User-Agent": UA, Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", Referer: LEMONCAMS_URL };

  // Scrape tags/categories from main pages
  const pages = [
    "https://www.lemoncams.com/",
    "https://www.lemoncams.com/tags",
    "https://www.lemoncams.com/categories",
    "https://www.lemoncams.com/pornstars",
  ];

  for (const url of pages) {
    try {
      const r = await fetch(proxiedUrl(url, proxy), { headers, signal: AbortSignal.timeout(15000) });
      if (!r.ok) { notes.push(`lemoncams-tags: ${url} -> HTTP ${r.status}`); continue; }
      const html = await r.text();

      // Extract tags from tag cloud / sidebar (href="/tags/...")
      const tagMatches = [...html.matchAll(/href="\/tags\/([^"]+)"/gi)];
      for (const m of tagMatches) tags.add(m[1].replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase()));

      // Extract categories from sidebar (href="/categories/...")
      const catMatches = [...html.matchAll(/href="\/categories\/([^"]+)"/gi)];
      for (const m of catMatches) categories.add(m[1].replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase()));

      // Extract genres (if separate) from sidebar
      const genreMatches = [...html.matchAll(/href="\/genres\/([^"]+)"/gi)];
      for (const m of genreMatches) genres.add(m[1].replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase()));

      // Also extract from data attributes in cam cards (data-tags, data-categories, data-haircolor, data-body)
      const dataTagMatches = [...html.matchAll(/data-tags=["']([^"']+)["']/gi)];
      for (const m of dataTagMatches) m[1].split(",").forEach(t => tags.add(t.trim()));

      const dataCatMatches = [...html.matchAll(/data-categories=["']([^"']+)["']/gi)];
      for (const m of dataCatMatches) m[1].split(",").forEach(c => categories.add(c.trim()));

      const hairColorMatches = [...html.matchAll(/haircolor:\s*([^,\n]+)/gi)];
      for (const m of hairColorMatches) hairColors.add(m[1].trim().replace(/\b\w/g, c => c.toUpperCase()));

      const bodyMatches = [...html.matchAll(/body:\s*([^,\n]+)/gi)];
      for (const m of bodyMatches) bodyTypes.add(m[1].trim().replace(/\b\w/g, c => c.toUpperCase()));

    } catch (e) {
      notes.push(`lemoncams-tags: ${url} -> ${e instanceof Error ? e.message : e}`);
    }
  }

  return {
    tags: Array.from(tags).sort(),
    categories: Array.from(categories).sort(),
    genres: Array.from(genres).sort(),
    hairColors: Array.from(hairColors).sort(),
    bodyTypes: Array.from(bodyTypes).sort(),
    notes,
  };
}

class CamHttpError extends Error { constructor(public status: number, msg: string) { super(msg); } }

async function getJson(url: string, headers: Record<string, string>, ms = 12000): Promise<any> {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    const r = await fetch(url, { headers, signal: ac.signal, redirect: "follow" });
    if (r.status >= 400) throw new CamHttpError(r.status, `HTTP ${r.status}`);
    const text = await r.text();
    // a blocked Worker IP gets an HTML challenge page with status 200: say so instead of "Unexpected token <"
    if (/^\s*</.test(text.slice(0, 200))) throw new CamHttpError(403, /just a moment|cf-chl|captcha|attention required/i.test(text.slice(0, 4000)) ? "bot challenge" : "HTML instead of JSON");
    return JSON.parse(text);
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") throw new CamHttpError(408, "timeout");
    throw e;
  } finally { clearTimeout(t); }
}

/** one retry (with jitter) for the failures that are usually momentary: 429, 5xx, timeouts, dropped connections */
async function getJsonRetry(url: string, headers: Record<string, string>, ms = 12000): Promise<any> {
  try { return await getJson(url, headers, ms); } catch (e) {
    const st = e instanceof CamHttpError ? e.status : 0;
    const transient = st === 408 || st === 429 || st >= 500 || (!st && /network|fetch failed|connection/i.test(String((e as Error)?.message)));
    if (!transient) throw e;
    await new Promise((r) => setTimeout(r, 300 + Math.random() * 600));
    return getJson(url, headers, ms);
  }
}

async function fetchDirect(name: string, spec: Spec): Promise<{ cams: any[]; notes: string[] }> {
  const base = spec.base, notes: string[] = [];
  const headers = { "User-Agent": UA, Accept: "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", Referer: base + "/", Origin: base, "X-Requested-With": "XMLHttpRequest" };
  for (const cand of spec.candidates) {
    // every page of a candidate at once (was one after the other: 3 slow pages = 36 s); results are read in page order
    const settled = await Promise.allSettled(cand.map((u) => getJsonRetry(u, headers)));
    const cams: any[] = [], seen = new Set<string>();
    settled.forEach((s, i) => {
      const where = cand[i].split("?")[0];
      if (s.status === "rejected") { notes.push(`${name}: ${where} -> ${s.reason instanceof Error ? s.reason.message : s.reason}`); return; }
      let fresh = 0;
      for (const d of roomList(s.value)) {
        const cam = camFromDict(d, base, name, false);
        if (cam && !seen.has(cam.username.toLowerCase())) { seen.add(cam.username.toLowerCase()); cams.push(cam); fresh++; }
      }
      if (!fresh) notes.push(`${name}: ${where} returned no usable rooms`);
    });
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

/** Fetch every direct platform + Lemoncams in parallel (each independent). Returns a payload even when some platforms fail.
 *  Every cam carries `_seen` (unix seconds) so a cam carried over from an older copy can be expired by age. */
export async function fetchLiveCams(opts: { wm?: string; providers?: string; proxy?: string } = {}): Promise<any> {
  const specs = directSpecs(opts.wm || "dvafl");
  const want = (opts.providers || "").split(",").map((x) => x.trim().toLowerCase()).filter((x) => x in specs);
  const names = want.length ? want : Object.keys(specs);
  const t0 = Date.now();
  const timed = await Promise.all(names.map(async (n) => {
    const t = Date.now();
    const r = await fetchDirect(n, specs[n]).catch((e) => ({ cams: [] as any[], notes: [`${n}: ${e instanceof Error ? e.message : e}`] }));
    return { ...r, ms: Date.now() - t };
  }));

  // Also fetch from Lemoncams (aggregates many platforms)
  const lemonResult = await fetchLemoncams(opts.proxy).catch((e) => ({ cams: [] as any[], notes: [`lemoncams: ${e instanceof Error ? e.message : e}`] }));
  const now = Math.floor(Date.now() / 1000);

  // Add lemoncams cams with _seen timestamp
  for (const c of lemonResult.cams) c._seen = now;

  const groups: Record<string, any[]> = {}, diag: string[] = [], status: Record<string, { ok: boolean; count: number; ms: number }> = {};
  names.forEach((n, i) => {
    for (const c of timed[i].cams) c._seen = now;
    groups[n] = timed[i].cams; diag.push(...timed[i].notes);
    status[n] = { ok: timed[i].cams.length > 0, count: timed[i].cams.length, ms: timed[i].ms };
  });
  // Add lemoncams as a separate source
  if (lemonResult.cams.length) {
    groups["lemoncams"] = lemonResult.cams;
    diag.push(...lemonResult.notes);
    status["lemoncams"] = { ok: true, count: lemonResult.cams.length, ms: 0 };
  }
  const items = merge(groups);
  if (!items.length) return { page: CAMS_HOME, items: [], count: 0, platform_status: status, diagnostics: diag, error: "No live cams could be loaded: none of the cam platforms answered." };
  const by: Record<string, number> = {};
  for (const c of items) { const k = c.provider_name || c.provider || "?"; by[k] = (by[k] || 0) + 1; }
  return { page: CAMS_HOME, items, count: items.length, providers: by, platform_status: status, source: "direct-worker+lemoncams", took_ms: Date.now() - t0, diagnostics: diag, fetched_at: now, _ts: now };
}

/** Fetch tags, categories, and genres from Lemoncams for filter UI. */
export async function fetchLemoncamsFilters(proxy: string = ""): Promise<{ tags: string[]; categories: string[]; genres: string[]; hairColors: string[]; bodyTypes: string[]; notes: string[] }> {
  return fetchLemoncamsTags(proxy);
}
