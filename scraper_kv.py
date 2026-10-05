#!/usr/bin/env python3
"""
Cloudflare KV Scraper - Runs in GitHub Actions, writes scraped data to Cloudflare KV.
This replaces the in-memory caching and threading from the original Flask app.
"""

import os
import sys
import json
import time
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote_plus

import httpx
import requests

# Add local backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import (
    scrape_page, scrape_categories, scrape_tags, scrape_studio_sections,
    scrape_superporn_categories, scrape_models, search_many, fetch_html,
    _current_page_number, _guess_next_page, _find_next_page_link,
)
from sourcetest import TEST_SITES, search_site, site_from_url
from extras import scrape_plus, deep_resolve, is_smart
try:
    from livecams import fetch_livecams
except Exception:
    fetch_livecams = None
try:
    from channels import fetch_channels
except Exception:
    fetch_channels = None
try:
    from metadata import fetch_metadata
except Exception:
    fetch_metadata = None


# Cloudflare KV API client
class KVClient:
    def __init__(self, account_id: str, api_token: str, namespace_id: str):
        self.account_id = account_id
        self.api_token = api_token
        self.namespace_id = namespace_id
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def put(self, key: str, value: dict, expiration_ttl: int = 3600) -> bool:
        """Write to KV with TTL in seconds."""
        url = f"{self.base_url}/values/{key}"
        params = {"expiration_ttl": expiration_ttl} if expiration_ttl else {}
        resp = self.session.put(url, json=value, params=params)
        return resp.status_code in (200, 201)

    def put_batch(self, entries: list[tuple[str, dict]], expiration_ttl: int = 3600) -> int:
        """Write multiple entries. Returns success count."""
        success = 0
        for key, value in entries:
            if self.put(key, value, expiration_ttl):
                success += 1
            time.sleep(0.05)  # Rate limit
        return success

    def get(self, key: str) -> dict | None:
        url = f"{self.base_url}/values/{key}"
        resp = self.session.get(url)
        if resp.status_code == 200:
            return resp.json()
        return None


# Global KV client (initialized in main)
kv_cache: KVClient | None = None
kv_scrape: KVClient | None = None


def init_kv_clients():
    global kv_cache, kv_scrape
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cache_ns = os.environ.get("CACHE_KV_NAMESPACE_ID")
    scrape_ns = os.environ.get("SCRAPE_DATA_KV_NAMESPACE_ID")

    if not all([account_id, api_token, cache_ns, scrape_ns]):
        print("Missing Cloudflare KV credentials", file=sys.stderr)
        return False

    kv_cache = KVClient(account_id, api_token, cache_ns)
    kv_scrape = KVClient(account_id, api_token, scrape_ns)
    return True


def kv_put_cache(key: str, value: dict, ttl: int = 3600):
    if kv_cache:
        kv_cache.put(key, value, ttl)


def kv_put_scrape(key: str, value: dict, ttl: int = 86400):
    if kv_scrape:
        kv_scrape.put(key, value, ttl)


def translate_result(r: dict) -> dict:
    """Placeholder for translation - can be extended."""
    return r


# Scrape functions that write to KV
def scrape_and_cache_url(url: str, page_num: int | None = None, fresh: bool = False):
    key = f"scrape:{url}"
    if not fresh:
        # Check cache first (in real usage, Worker checks cache)
        pass

    try:
        if is_smart(url):
            result = scrape_plus(url, max_items=80, page_num=page_num)
        else:
            result = scrape_page(url, max_items=80, page_num=page_num)

        result = translate_result(result)
        kv_put_scrape(key, result, ttl=86400)  # 24 hours
        print(f"  Cached: {url} ({result.get('count', 0)} items)")
        return result
    except Exception as e:
        error_result = {"page": url, "items": [], "count": 0, "error": str(e), "next_page": None, "page_num": page_num or 1}
        kv_put_scrape(key, error_result, ttl=3600)
        print(f"  Error caching {url}: {e}")
        return error_result


def scrape_search_and_cache(sites: list[str], query: str, verify: bool = False, max_items: int = 40):
    cache_key = f"search:{','.join(sorted(sites))}:{query}:{verify}:{max_items}"

    try:
        normal = [s for s in sites if not is_smart(s)]
        plus = [s for s in sites if is_smart(s)]

        results = []
        if normal:
            results.extend(search_many(normal, query, max_items=max_items, verify=verify))
        for site in plus:
            results.append(search_site(site_from_url(site), query))

        # Build combined results like the original API
        combined, seen = [], set()
        for r in results:
            site_name = r.get("site") or r.get("page") or ""
            for it in (r.get("items") or []):
                k = (it.get("link") or "").split("?")[0]
                if not k or k in seen:
                    continue
                seen.add(k)
                combined.append({**it, "_site": site_name})

        payload = {
            "results": [translate_result(r) for r in results],
            "query": query,
            "combined": combined,
            "count": len(combined),
        }
        kv_put_scrape(cache_key, payload, ttl=86400)
        print(f"  Cached search: {query} across {len(sites)} sites ({len(combined)} combined)")
        return payload
    except Exception as e:
        error_payload = {"results": [], "query": query, "combined": [], "count": 0, "error": str(e)}
        kv_put_scrape(cache_key, error_payload, ttl=3600)
        return error_payload


def scrape_resolve_and_cache(url: str, full: bool = False):
    key = f"resolve-full:{url}" if full else f"resolve:{url}"
    try:
        if is_smart(url) or full:
            result = deep_resolve(url)
        else:
            result = {"video": None, "error": "Use full resolve for this site"}
        kv_put_scrape(key, result, ttl=86400)
        print(f"  Cached resolve: {url}")
        return result
    except Exception as e:
        error_result = {"video": None, "error": str(e)}
        kv_put_scrape(key, error_result, ttl=3600)
        return error_result


def scrape_metadata_and_cache(url: str):
    key = f"meta:{url}"
    if not fetch_metadata:
        return {"url": url, "groups": [], "error": "metadata module unavailable"}

    try:
        html, final = fetch_html(url, timeout=20.0)
        data = fetch_metadata(html, final)
        kv_put_scrape(key, data, ttl=86400)
        print(f"  Cached metadata: {url}")
        return data
    except Exception as e:
        error_data = {"url": url, "groups": [], "error": str(e)}
        kv_put_scrape(key, error_data, ttl=3600)
        return error_data


def scrape_livecams_and_cache(url: str | None = None, force: bool = False):
    key = f"livecams:{url or 'default'}:{force}"
    if not fetch_livecams:
        return {"items": [], "count": 0, "error": "livecams module unavailable"}

    try:
        data = fetch_livecams(url, force=force)
        kv_put_scrape(key, data, ttl=1800)  # 30 min
        print(f"  Cached livecams: {len(data.get('items', []))} items")
        return data
    except Exception as e:
        error_data = {"items": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data, ttl=600)
        return error_data


def scrape_channels_and_cache(category: str | None = None, page: int = 1, force: bool = False):
    key = f"channels:{category or 'all'}:{page}:{force}"
    if not fetch_channels:
        return {"items": [], "count": 0, "error": "channels module unavailable"}

    try:
        data = fetch_channels(category, page, force=force)
        kv_put_scrape(key, data, ttl=1800)
        print(f"  Cached channels: {len(data.get('items', []))} items")
        return data
    except Exception as e:
        error_data = {"items": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data, ttl=600)
        return error_data


def scrape_categories_and_cache(url: str, mode: str = "categories", page_num: int | None = None):
    key = f"categories:{url}:{mode}:{page_num or 1}"
    try:
        if mode in ("models", "pornstars"):
            data = scrape_models(url, page_num=page_num)
        elif mode == "sites":
            data = scrape_studio_sections(url)
        elif mode == "tags":
            data = scrape_tags(url, kind="porntags")
        else:
            data = scrape_categories(url, page_num=page_num)
        data = translate_result(data)
        kv_put_scrape(key, data, ttl=86400)
        print(f"  Cached categories: {url} ({data.get('count', 0)} items)")
        return data
    except Exception as e:
        error_data = {"categories": [], "count": 0, "error": str(e)}
        kv_put_scrape(key, error_data, ttl=3600)
        return error_data


# Main scraping jobs
def job_catalog_urls():
    """Write static catalog URLs to KV."""
    catalog = {
        "categories": "https://www.superporn.com/categories",
        "pornstars": "https://www.freesexvideos.xxx/models/",
        "studios": "https://www.freesexvideos.xxx/sites/",
        "bdsmStudios": "https://www.bdsmhole.com/studios/",
        "liveCams": "https://www.lemoncams.com/",
    }
    kv_put_scrape("catalog:urls", catalog, ttl=86400 * 7)
    print("Cached catalog URLs")


def job_test_sites():
    """Write test sites list to KV."""
    sites = [{"id": x["id"], "name": x["name"], "feed": x["feed"]} for x in TEST_SITES]
    kv_put_scrape("test-sites", {"sites": sites}, ttl=86400 * 7)
    print(f"Cached {len(sites)} test sites")


def job_popular_feeds():
    """Scrape popular feed URLs (first page of each test site)."""
    urls = [site["feed"] for site in TEST_SITES]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(scrape_and_cache_url, url) for url in urls]
        for f in as_completed(futures):
            f.result()


def job_category_pages():
    """Scrape category/tag/studio listing pages."""
    category_urls = [
        ("https://www.superporn.com/categories", "categories"),
        ("https://www.freesexvideos.xxx/models/", "models"),
        ("https://www.freesexvideos.xxx/sites/", "sites"),
        ("https://www.bdsmhole.com/studios/", "categories"),
    ]
    for url, mode in category_urls:
        scrape_categories_and_cache(url, mode)
        time.sleep(1)


def job_search_queries():
    """Pre-scrape common search queries across popular sites."""
    popular_sites = [s["feed"] for s in TEST_SITES[:4]]
    queries = ["milf", "teen", "anal", "amateur", "lesbian", "mature", "big tits", "blowjob"]

    for query in queries:
        scrape_search_and_cache(popular_sites, query, verify=False, max_items=40)
        time.sleep(2)


def job_livecams_channels():
    """Scrape livecams and channels."""
    scrape_livecams_and_cache()
    scrape_channels_and_cache()
    time.sleep(2)


def job_resolve_sample():
    """Resolve a sample of video URLs from recent scrapes."""
    # This would read from KV and resolve links - simplified for now
    pass


def main():
    if not init_kv_clients():
        sys.exit(1)

    print("Starting Cloudflare KV scraper...")
    start = time.time()

    # Static data
    job_catalog_urls()
    job_test_sites()

    # Dynamic scraping
    job_popular_feeds()
    job_category_pages()
    job_search_queries()
    job_livecams_channels()

    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.1f}s")


if __name__ == "__main__":
    main()