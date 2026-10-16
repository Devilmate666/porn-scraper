# BDSM Hole Tags Integration

## Summary
Added BDSM Hole tags scraping with automatic deduplication against categories to ensure no repeated tags between categories and tags.

## Changes Made

### 1. Python Scraper (scraper.py)
- Added `scrape_bdsmhole_tags()` function to scrape BDSM Hole tags from /tags/ page
- Extracts tags with /tag/<slug>/ URL pattern
- Returns tags in the same format as other tag scrapers

### 2. Python KV Scraper (scraper_kv.py)
- Imported `scrape_bdsmhole_tags` from scraper module
- Added BDSM Hole tags URL to LISTINGS configuration
- Updated `scrape_categories_and_cache()` to handle BDSM Hole tags with deduplication:
  - First fetches categories from BDSM Hole
  - Extracts category names (lowercase)
  - Filters out tags that match category names before returning
- This ensures no duplicate tags appear in both categories and tags

### 3. Flask Backend (app.py)
- Imported `scrape_bdsmhole_tags` from scraper module
- Added BDSM Hole tags handler in `/api/scrape-categories` endpoint
- Implements same deduplication logic as scraper_kv.py:
  - Fetches categories first
  - Filters tags against category names
  - Returns deduplicated tag list

### 4. TypeScript Listings (listings.ts)
- Added `scrapeBdsmholeTags()` function (TypeScript port of Python version)
- Added optional `excludeCategories` parameter for deduplication
- Updated `scrapeListing()` dispatcher to handle BDSM Hole tags
- Implements async deduplication:
  - Fetches categories first
  - Creates Set of category names (lowercase)
  - Passes to `scrapeBdsmholeTags()` for filtering

## Deduplication Logic

The system now ensures:
1. **Categories are mixed**: All categories from all sites are combined
2. **Tags are mixed**: All tags from all sites are combined
3. **BDSM Hole has its own filter**: BDSM Hole tags are filtered to remove duplicates with BDSM Hole categories
4. **No repeated tags**: Tags that match category names (case-insensitive) are removed from the tag list

This follows the requirement:
- All categories mixed ✓
- All tags mixed ✓
- BDSM Hole gets its BDSM filter in both (categories + tags) ✓
- No repeated tags or categories between categories and tags ✓

## BDSM Hole Categories (for reference)
From https://www.bdsmhole.com/categories/:
- 18 Year Old, Anal, Asian, BDSM, Black, Bondage, Corporal Punishment, Creampie, Cuckolding, Domination, Double Anal, Double Penetration, Feet, Femdom, Fetish, Fisting, Gangbang, Gay, Humiliation, Latex, Lesbian, MILF, Pain, Pegging, Public Fuck, Role Play, Slave, Sounding, Speculum, Squirting, Strap On, Submission, Sybian, Threesome, Tranny, Wrestling

These category names will be filtered out from the BDSM Hole tags list automatically.

## Testing
To test the changes:
1. Run the scraper: `python scraper_kv.py` (or trigger via GitHub Actions)
2. The BDSM Hole tags will be scraped and cached
3. Tags that match category names will be automatically filtered out
4. The deduplicated tag list will be available via the API

## API Endpoint
- `POST /api/scrape-categories` with `{"url": "https://www.bdsmhole.com/tags/", "mode": "tags"}`
- Returns deduplicated tag list
