from app import app

# All your API routes (/api/scrape, /api/stream, /api/search, etc.)
# are already defined in app.py
# Cloudflare Functions will route any /api/* request to this Flask app

# This file is minimal — it just imports your Flask app
# Cloudflare will automatically handle the /api prefix
