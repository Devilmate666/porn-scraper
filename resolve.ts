// ---------------------------------------------------------------------------
// Live video-link resolver: port of resolve_video_url / resolve_full_video_url from scraper.py.
// ---------------------------------------------------------------------------
import { HTMLElement } from "node-html-parser";
import { absUrl, attr, clsOf, collapse, fetchHtml, isVideoUrl, IMAGE_EXT, PLACEHOLDER, SHARED_IMAGE, prepareFull, UA } from "./scrape";

const EXT = "(?:mp4|m3u8|webm|mov|mpd)";
const PATTERNS: RegExp[] = [
  new RegExp(`<video[^>]+src=["']([^"']+?\\.${EXT}[^"']*)["']`, "gis"),
  new RegExp(`<source[^>]+src=["']([^"']+?\\.${EXT}[^"']*)["']`, "gis"),
  /<meta[^>]+(?:property|name)=["'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["'][^>]+content=["']([^"']+)["']/gis,
  new RegExp(`"contentUrl"\\s*:\\s*"([^"]+\\.${EXT}[^"]*)"`, "gis"),
  new RegExp(`"embedUrl"\\s*:\\s*"([^"]+\\.${EXT}[^"]*)"`, "gis"),
  new RegExp(`(?:file|video_url|videoUrl|videoSrc|hls_url|mp4_url)\\s*[:=]\\s*["']([^"']+?\\.${EXT}[^"']*)["']`, "gis"),
  new RegExp(`sources\\s*:\\s*\\[\\s*\\{[^}]*?"file"\\s*:\\s*"([^"]+\\.${EXT}[^"]*)"`, "gis"),
  new RegExp(`["'](?:hls|mp4|source)["']\\s*:\\s*["']([^"']+?\\.${EXT}[^"']*)["']`, "gis"),
  new RegExp(`["'](https?://[^"']+?\\.${EXT}(?:/|\\?[^"']*)?)["']`, "gis"),
  new RegExp(`["'](//[^"']+?\\.${EXT}(?:/|\\?[^"']*)?)["']`, "gis"),
  /(https?:\/\/[^"'\s<>]+\/get_file\/[^"'\s<>]+?\.(?:mp4|webm|mov)(?:\/|\?[^"'\s<>]*)?)/gis,
];

export const isImage = (u: string) => IMAGE_EXT.test(u.split("?")[0]);

export function classify(u: string): string {
  const low = u.toLowerCase().split("?")[0].replace(/\/+$/, "");
  if (/\.(mp4|m4v|mov)$/.test(low)) return "mp4";
  if (low.endsWith(".webm")) return "webm";
  if (low.endsWith(".m3u8") || u.toLowerCase().includes(".m3u8")) return "hls";
  if (low.endsWith(".mpd")) return "dash";
  if (low.includes("/get_file/") || low.includes("/get_stream/")) return "mp4";
  return "other";
}

export function collectFromHtml(html: string, base: string): string[] {
  const found: string[] = [];
  for (const re of PATTERNS) {
    re.lastIndex = 0;
    for (const m of html.matchAll(re)) {
      let u = (m[1] || "").replace(/\\\//g, "/").trim();
      if (u.startsWith("//")) u = "https:" + u;
      else if (!/^https?:/i.test(u)) { const a = absUrl(base, u); if (!a) continue; u = a; }
      if (isImage(u) || !isVideoUrl(u)) continue;
      found.push(u);
    }
  }
  return found;
}

const isPreviewMedia = (url: string) => {
  const low = (url || "").toLowerCase();
  if (low.includes("/preview/") || (low.includes("cast.") && low.includes("preview"))) return true;
  return /(?:preview|previews|thumbnail|thumb|poster|teaser|sample|trailer|sprite|storyboard|hover|lowres|low-res|low_quality|small)(?:[._\-/]|$)/.test(low);
};

export function scoreCandidate(item: { src: string; type: string }): number {
  const low = (item.src || "").toLowerCase();
  let s = 0;
  if (isPreviewMedia(item.src)) s -= 1000;
  s += ({ mp4: 30, hls: 25, dash: 20, webm: 15 } as Record<string, number>)[item.type] || 0;
  if (low.includes("/get_file/")) s += 80;
  const q = low.match(/_(2160|1440|1080|720|480|360)p?(?:m)?\.mp4/);
  if (q) { s += 40; s += parseInt(q[1], 10) / 10; }
  if (/(full|original|source|master|playlist)/.test(low)) s += 20;
  if (/(preview|thumb|poster|sample|teaser|trailer)/.test(low)) s -= 500;
  if (low.includes("dreamserve")) s -= 50;
  return s;
}

// ok.ru embeds (used by bdsmhole-style players)
const OK_KEY = "CBAFJIICABABABABA";
const OK_SESSION = "-s-280i1-fBE732Y5lav2k7Z3hcFfpi.2-Yl2272eEcH13hc";
const OK_TAGS = ["url_ultrahd", "url_quadhd", "url_fullhd", "url_high", "url_medium", "url_low", "url_mobile", "url_tiny"];
async function okRu(html: string, detail: string): Promise<{ src: string; type: string }[]> {
  const m = html.match(/generate_mp4\s*\(\s*['"][^'"]+['"]\s*,\s*['"][^'"]+['"]\s*,\s*['"]?(\d{8,})['"]?/i) || html.match(/generate_mp4\s*\([^)]*?(\d{10,})[^)]*\)/i);
  if (!m) return [];
  const api = "https://api.ok.ru/fb.do?application_key=" + OK_KEY + "&fields=" +
    encodeURIComponent("video.url_tiny,video.url_low,video.url_high,video.url_medium,video.url_quadhd,video.url_mobile,video.url_ultrahd,video.url_fullhd").replace(/%2C/g, "%2C") +
    "&method=video.get&session_key=" + OK_SESSION + "&vids=" + m[1];
  let text = "";
  try {
    const r = await fetch(api, { headers: { "User-Agent": UA, Referer: detail || "https://www.bdsmhole.com/", Origin: "https://www.bdsmhole.com", Accept: "application/xml,text/xml,*/*" } });
    if (!r.ok) return [];
    text = await r.text();
  } catch { return []; }
  const out: { src: string; type: string }[] = [];
  for (const tag of OK_TAGS) {
    for (const mm of text.matchAll(new RegExp(`<${tag}>\\s*(https?://[^<\\s]+)\\s*</${tag}>`, "gi"))) {
      const u = mm[1].replace(/&amp;/g, "&").trim();
      if (u && !isImage(u)) out.push({ src: u, type: "mp4" });
    }
    if (out.length) break;
  }
  const seen = new Set<string>();
  return out.filter((x) => { const k = x.src.split("?")[0]; if (seen.has(k)) return false; seen.add(k); return true; });
}

function detailThumbnail(root: HTMLElement, base: string): string | null {
  const ok = (u: string | null) => !!u && !isVideoUrl(u) && !PLACEHOLDER.test(u) && !SHARED_IMAGE.test(u);
  for (const prop of ["og:image:secure_url", "og:image:url", "og:image", "twitter:image", "twitter:image:src"]) {
    const tag = root.querySelector(`meta[property="${prop}"]`) || root.querySelector(`meta[name="${prop}"]`);
    if (tag && attr(tag, "content")) { const u = absUrl(base, attr(tag, "content")); if (ok(u)) return u; }
  }
  for (const sc of root.querySelectorAll("script")) {
    if (!/ld\+json/i.test(attr(sc, "type"))) continue;
    try {
      const stack: any[] = [JSON.parse(sc.rawText || "")];
      while (stack.length) {
        const x = stack.pop();
        if (Array.isArray(x)) stack.push(...x);
        else if (x && typeof x === "object") {
          for (const k of ["thumbnailUrl", "image", "thumbnail"]) {
            let v = x[k];
            if (Array.isArray(v) && v.length) v = v[0];
            if (v && typeof v === "object") v = v.url || v.contentUrl;
            if (typeof v === "string") { const u = absUrl(base, v); if (ok(u)) return u; }
          }
          stack.push(...Object.values(x));
        }
      }
    } catch { /* bad json-ld */ }
  }
  for (const v of root.querySelectorAll("video")) { const u = absUrl(base, attr(v, "poster") || attr(v, "data-poster")); if (ok(u)) return u; }
  let best: string | null = null, bestScore = 0;
  for (const img of root.querySelectorAll("img")) {
    let u: string | null = null;
    for (const a of ["data-src", "data-original", "data-lazy", "data-thumb", "data-poster", "data-cover", "src"]) {
      const c = absUrl(base, attr(img, a)); if (ok(c)) { u = c; break; }
    }
    if (!u) continue;
    let score = Math.max(parseInt(attr(img, "width")) || 0, parseInt(attr(img, "height")) || 0, 1);
    if (/(thumb|poster|preview|cover)/.test(u.toLowerCase())) score += 1000;
    if (score > bestScore) { best = u; bestScore = score; }
  }
  return best;
}

export async function resolveVideo(detailUrl: string, light = false): Promise<any> {
  let html: string, base: string;
  try {
    const r = await fetchHtml(detailUrl, light ? 10000 : 25000);
    html = r.html; base = r.final;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return { error: msg, detail_url: detailUrl, unavailable: /404|410/.test(msg), video: null, all: [], thumbnail: null };
  }
  const root = prepareFull(html);
  const candidates = collectFromHtml(html, base);
  let iframeFailed = false;
  const skip = light && candidates.some((u) => !isImage(u) && classify(u) !== "other" && !isPreviewMedia(u));
  if (!skip) {
    const frames = root.querySelectorAll("iframe").map((f) => attr(f, "src")).filter(Boolean).slice(0, 4);
    await Promise.all(frames.map(async (src) => {
      const player = absUrl(base, src);
      if (!player) return;
      try {
        const ac = new AbortController();
        const t = setTimeout(() => ac.abort(), light ? 6000 : 15000);
        const r = await fetch(player, { headers: { "User-Agent": UA, Referer: detailUrl }, signal: ac.signal });
        clearTimeout(t);
        candidates.push(...collectFromHtml(await r.text(), player));
      } catch { iframeFailed = true; }
    }));
  }
  const seen = new Set<string>();
  let out: { src: string; type: string }[] = [];
  for (const u of candidates) {
    if (isImage(u)) continue;
    const key = u.split("?")[0];
    if (seen.has(key)) continue;
    seen.add(key);
    let t = classify(u);
    if (t === "other") { if (/okcdn\.ru|ok\.ru/i.test(u)) t = "mp4"; else continue; }
    out.push({ src: u, type: t });
  }
  const ok = await okRu(html, detailUrl);
  if (ok.length) {
    const okSrc = new Set(ok.map((x) => x.src.split("?")[0]));
    out = ok.concat(out.filter((x) => !okSrc.has(x.src.split("?")[0]) && !x.src.toLowerCase().includes("/get_stream/")));
  }
  const downloads: any[] = [];
  for (const a of root.querySelectorAll("a[href]")) {
    const href = attr(a, "href").trim();
    if (!href || /^(#|javascript:)/i.test(href)) continue;
    const full = absUrl(base, href);
    if (!full || !["mp4", "webm"].includes(classify(full)) || isImage(full)) continue;
    if (full.toLowerCase().includes("download=") || a.hasAttribute("download") || clsOf(a).includes("download")) {
      downloads.push({ src: full, label: collapse(a.text || "").slice(0, 40), attach_session: attr(a, "data-attach-session") || null });
    }
  }
  return {
    detail_url: detailUrl,
    thumbnail: detailThumbnail(root, base),
    video: out[0] || null,
    all: out,
    downloads,
    unavailable: !out.length && !iframeFailed,
  };
}

export async function resolveFull(detailUrl: string): Promise<any> {
  const r = await resolveVideo(detailUrl, false);
  const cands: { src: string; type: string }[] = r.all || [];
  if (!cands.length) return { ...r, video: null, full_video: null };
  const ranked = [...cands].sort((a, b) => scoreCandidate(b) - scoreCandidate(a));
  const best = scoreCandidate(ranked[0]) > -900 ? ranked[0] : null;
  return { ...r, video: best, full_video: best };
}
