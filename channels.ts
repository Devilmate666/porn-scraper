// ---------------------------------------------------------------------------
// Live TV channels (xlivetv.com) scraped by the Worker itself - TypeScript port of channels.py.
// Used when KV has no channel data yet, so "Live TV" never depends on a GitHub run having succeeded.
//   /  or  /categories/<slug>/   channel grid (12 per page)       /page/N/  more pages
//   /<slug>/                     one channel: description, categories, og:image
// Result shape = channels.py fetch_channels(): {page, items[], count, category, total_pages, next_page, categories[]}
// ---------------------------------------------------------------------------
import { parse, HTMLElement } from "node-html-parser";

const BASE = "https://xlivetv.com/";
const HOST = "xlivetv.com";
const NON_CHANNEL = new Set(["categories", "favorites", "games", "page", "contact", "terms-of-use", "privacy-policy",
  "disclaimer", "assets", "cdn-cgi", "search", "about", "dmca", "sitemap", "sitemap.xml", "feed.xml", "robots.txt",
  "tag", "tags", "blog", "favicon.ico"]);
const CAT_RX = /^[a-z0-9][a-z0-9-]{0,60}$/;
const PAGE_RX = /\/page\/(\d+)\/?$/;
const LAZY_ATTRS = ["data-src", "data-original", "data-lazy-src", "data-lazy", "data-thumb", "data-image", "data-bg", "data-background"];
const JUNK_IMG = /hits\.sh|favicon|pixel|spacer|blank/i;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

// ------------------------------------------------------------------ helpers
const abs = (base: string, u?: string | null): string | null => {
  const v = (u || "").trim();
  if (!v || /^(#|javascript:|mailto:|tel:|data:)/i.test(v)) return null;
  try { return new URL(v, base).toString().split("#")[0]; } catch { return null; }
};
const sameHost = (u: string) => { try { const h = new URL(u).hostname.toLowerCase(); return h === HOST || h.endsWith("." + HOST); } catch { return false; } };
const segments = (u: string) => { try { return new URL(u).pathname.split("/").filter(Boolean); } catch { return []; } };
const collapse = (s: string) => (s || "").replace(/\s+/g, " ").trim();
const txt = (el: HTMLElement | null | undefined) => (el ? collapse(el.text) : "");
const pretty = (slug: string) => slug.split("-").map((w) => (["tv", "hd", "xxx"].includes(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");

export function channelSlug(u: string | null): string | null {
  if (!u || !sameHost(u)) return null;
  const seg = segments(u);
  return seg.length === 1 && !NON_CHANNEL.has(seg[0].toLowerCase()) && !seg[0].includes(".") ? seg[0] : null;
}

function fromSrcset(v: string | null | undefined, base: string): string | null {
  let best: string | null = null, bw = -1;
  for (const part of (v || "").split(",")) {
    const bits = part.trim().split(/\s+/);
    if (!bits[0]) continue;
    const m = bits[1] ? /^(\d+)/.exec(bits[1]) : null;
    const w = m ? parseInt(m[1], 10) : 0;
    if (w > bw) { best = bits[0]; bw = w; }
  }
  return best ? abs(base, best) : null;
}

function imagesIn(container: HTMLElement, base: string): string[] {
  const out: string[] = [];
  const add = (u?: string | null) => {
    const a = abs(base, u);
    if (a && !JUNK_IMG.test(a) && !out.includes(a)) out.push(a);
  };
  for (const img of container.querySelectorAll("img")) {
    for (const a of LAZY_ATTRS) add(img.getAttribute(a));
    add(fromSrcset(img.getAttribute("data-srcset") || img.getAttribute("srcset"), base));
    add(img.getAttribute("src"));
  }
  for (const so of container.querySelectorAll("source")) add(fromSrcset(so.getAttribute("data-srcset") || so.getAttribute("srcset"), base));
  for (const el of [container, ...container.querySelectorAll("*")]) {
    const m = /url\(\s*['"]?([^'")]+)/.exec(el.getAttribute("style") || "");
    if (m) add(m[1]);
    for (const a of ["data-bg", "data-background", "data-bg-src", "data-image"]) add(el.getAttribute(a));
  }
  return out;
}

const isDescendant = (anc: HTMLElement, node: HTMLElement): boolean => {
  let p = node.parentNode as HTMLElement | null;
  while (p) { if (p === anc) return true; p = p.parentNode as HTMLElement | null; }
  return false;
};

/** lowest ancestor holding every anchor of the same channel = the card */
function cardContainer(anchors: HTMLElement[]): HTMLElement | null {
  const first = anchors[0];
  let anc = first.parentNode as HTMLElement | null;
  while (anc) {
    const tag = (anc.rawTagName || "").toLowerCase();
    if (!tag || tag === "body" || tag === "html") break;
    if (anchors.slice(1).every((a) => a === anc || isDescendant(anc as HTMLElement, a))) return anc;
    anc = anc.parentNode as HTMLElement | null;
  }
  return first.parentNode as HTMLElement | null;
}

// ------------------------------------------------------------------ listing page
export function parseListing(html: string, base = BASE): { items: any[]; maxPage: number } {
  const root = parse(html);
  root.querySelectorAll("script, style, noscript").forEach((e) => e.remove());
  const groups = new Map<string, HTMLElement[]>();
  for (const a of root.querySelectorAll("a[href]")) {
    const link = abs(base, a.getAttribute("href"));
    if (link && channelSlug(link)) {
      const k = link.split("?")[0].replace(/\/+$/, "") + "/";
      (groups.get(k) || groups.set(k, []).get(k)!).push(a);
    }
  }
  const items: any[] = [];
  for (const [link, anchors] of groups) {
    const slug = channelSlug(link)!;
    const card = cardContainer(anchors);
    let title = txt(card?.querySelector("h2, h3, h4"));
    if (!title) {
      for (const a of anchors) {
        const img = a.querySelector("img");
        let t = (a.getAttribute("title") || a.getAttribute("aria-label") || "").trim();
        t = t || (img ? (img.getAttribute("alt") || "").trim() : "");
        t = t || (!["watch now", "watch", "play"].includes(txt(a).toLowerCase()) ? txt(a) : "");
        if (t) { title = t; break; }
      }
    }
    title = title || pretty(slug);
    const imgs = card ? imagesIn(card, base) : [];
    const categories: { name: string; slug: string }[] = [];
    if (card) {
      for (const ca of card.querySelectorAll('a[href*="/categories/"]')) {
        const seg = segments(abs(base, ca.getAttribute("href")) || "");
        const cs = seg.length ? seg[seg.length - 1] : null;
        if (cs && CAT_RX.test(cs) && cs !== "categories") categories.push({ name: pretty(cs), slug: cs });
      }
    }
    items.push({ title, link, slug, thumbnail: imgs[0] || null, thumbnails: imgs.slice(0, 3), description: null, categories, provider: "xlivetv" });
  }
  let maxPage = 1;
  for (const a of root.querySelectorAll("a[href]")) {
    const m = PAGE_RX.exec(a.getAttribute("href") || "");
    if (m) maxPage = Math.max(maxPage, parseInt(m[1], 10));
  }
  return { items, maxPage };
}

// ------------------------------------------------------------------ one channel page (regex: cheap on CPU)
const decode = (s: string) => collapse(s.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#0?39;|&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " "));
const metaContent = (html: string, names: string[]): string | null => {
  for (const n of names) {
    const a = new RegExp(`<meta[^>]+(?:property|name)=["']${n}["'][^>]*content=["']([^"']*)["']`, "i").exec(html);
    const b = new RegExp(`<meta[^>]+content=["']([^"']*)["'][^>]*(?:property|name)=["']${n}["']`, "i").exec(html);
    const v = (a || b)?.[1];
    if (v && v.trim()) return decode(v);
  }
  return null;
};

export function parseChannel(html: string, url: string) {
  const body = html.replace(/<(script|style|noscript|svg)[\s\S]*?<\/\1>/gi, " ");
  const main = body.split(/<footer[\s>]/i)[0].replace(/<nav[\s\S]*?<\/nav>/gi, " ");
  const h1 = /<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(main);
  let best = "";
  for (const m of main.matchAll(/<p[^>]*>([\s\S]*?)<\/p>/gi)) {
    const t = decode(m[1]);
    if (t.length > best.length && !/please wait|cookie|all rights reserved|start typing/i.test(t)) best = t;
  }
  const ogDesc = metaContent(html, ["og:description", "description"]);
  let description: string | null = best || ogDesc;
  if (description && description.length < 25 && (ogDesc || "").length > description.length) description = ogDesc;
  const categories: { name: string; slug: string }[] = [];
  const seen = new Set<string>();
  for (const m of main.matchAll(/href=["']([^"']*\/categories\/([a-z0-9][a-z0-9-]*)\/?)["']/gi)) {
    const cs = m[2].toLowerCase();
    if (cs !== "categories" && CAT_RX.test(cs) && !seen.has(cs)) { seen.add(cs); categories.push({ name: pretty(cs), slug: cs }); }
  }
  return {
    title: h1 ? decode(h1[1]) || null : null,
    image: abs(url, metaContent(html, ["og:image", "twitter:image"])),
    description,
    categories,
  };
}

// ------------------------------------------------------------------ network
class HttpError extends Error { constructor(public status: number, msg: string) { super(msg); } }

async function getHtml(url: string, timeoutMs = 15000): Promise<{ html: string; final: string }> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", Referer: BASE },
      redirect: "follow", signal: ac.signal, cf: { cacheTtl: 300, cacheEverything: true },
    } as RequestInit);
    if (!r.ok) throw new HttpError(r.status, `HTTP ${r.status}`);
    const html = await r.text();
    if (/just a moment|cf-chl|attention required/i.test(html.slice(0, 3000)) && html.length < 20000) throw new HttpError(403, "bot challenge");
    return { html, final: r.url || url };
  } finally { clearTimeout(timer); }
}

let catMemo: { t: number; items: any[] } | null = null;
async function fetchCategories(): Promise<any[]> {
  if (catMemo && Date.now() - catMemo.t < 3600_000) return catMemo.items;
  const { html, final } = await getHtml(BASE + "categories/", 12000);
  const root = parse(html);
  const out: any[] = [], seen = new Set<string>();
  for (const a of root.querySelectorAll('a[href*="/categories/"]')) {
    const u = abs(final, a.getAttribute("href"));
    const seg = segments(u || "");
    if (!u || !sameHost(u) || seg.length !== 2 || seg[0] !== "categories" || seen.has(seg[1])) continue;
    const m = /^(.*?)\s*(\d+)\s*channels?$/i.exec(txt(a));
    seen.add(seg[1]);
    out.push({ slug: seg[1], name: pretty(seg[1]), count: m ? parseInt(m[2], 10) : null });
  }
  out.sort((a, b) => (b.count || 0) - (a.count || 0));
  if (out.length) catMemo = { t: Date.now(), items: out };
  return out;
}

/** read every channel page once (6 at a time): description, categories, og:image as thumbnail fallback */
async function enrich(items: any[], diag: string[]) {
  let ok = 0, next = 0;
  const deadline = Date.now() + 14000;
  const worker = async () => {
    while (next < items.length && Date.now() < deadline) {
      const it = items[next++];
      try {
        const { html, final } = await getHtml(it.link, 8000);
        const info = parseChannel(html, final);
        ok++;
        it.description = info.description || it.description;
        if (info.categories.length) it.categories = info.categories;
        if (info.title && info.title.length >= 2) it.title = info.title;
        if (info.image && !it.thumbnails.includes(info.image)) it.thumbnails.push(info.image);
      } catch (e: any) { diag.push(`channel ${it.slug}: ${String(e?.message || e).slice(0, 60)}`); }
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, items.length) }, worker));
  diag.push(`channel pages read: ${ok}/${items.length}`);
}

/** page of live TV channels; same JSON as channels.py fetch_channels() */
export async function fetchChannelsNative(category: string | null, page: number): Promise<any> {
  const cat = (category || "").trim().toLowerCase() || null;
  if (cat && !CAT_RX.test(cat)) return { items: [], count: 0, error: "invalid category", diagnostics: [] };
  const pg = Math.max(1, Math.floor(page) || 1);
  const base = cat ? `${BASE}categories/${cat}/` : BASE;
  const url = pg <= 1 ? base : `${base}page/${pg}/`;
  const diag: string[] = [];
  let html: string, final: string;
  try {
    ({ html, final } = await getHtml(url));
  } catch (e: any) {
    if (pg > 1 && e instanceof HttpError && e.status === 404) {
      return { page: pg, items: [], count: 0, next_page: null, total_pages: pg - 1, category: cat, end: true, diagnostics: [`${url}: 404`] };
    }
    return { page: pg, items: [], count: 0, next_page: null, category: cat, error: `Could not load ${url}: ${String(e?.message || e).slice(0, 120)}`, diagnostics: diag };
  }
  const { items, maxPage } = parseListing(html, final);
  diag.push(`${url}: ${items.length} channels, last page ${maxPage}`);
  await enrich(items, diag);
  for (const it of items) { it.thumbnails = it.thumbnails.filter(Boolean).slice(0, 4); it.thumbnail = it.thumbnails[0] || null; }
  let categories: any[] = [];
  if (pg === 1) { try { categories = await fetchCategories(); } catch (e: any) { diag.push(`categories: ${String(e?.message || e).slice(0, 60)}`); } }
  return {
    page: pg, items, count: items.length, category: cat, total_pages: maxPage,
    next_page: items.length && pg < maxPage ? pg + 1 : null,
    categories, diagnostics: diag, fetched_at: Math.floor(Date.now() / 1000),
  };
}
