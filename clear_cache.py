#!/usr/bin/env python3
"""Clear title-translation cache files in this folder.

Usage (from the oline folder):
    python3 clear_cache.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = (
    ".title_translate_cache.json",
    ".title_translate_cache.json.tmp",
)

removed = []
missing = []
for name in TARGETS:
    path = os.path.join(HERE, name)
    if os.path.isfile(path):
        try:
            os.remove(path)
            removed.append(name)
        except OSError as e:
            print(f"Failed to remove {name}: {e}")
            sys.exit(1)
    else:
        missing.append(name)

if removed:
    print("Removed:")
    for n in removed:
        print(f"  - {n}")
else:
    print("No cache files found (already clear).")

if missing and removed:
    print("Already absent:")
    for n in missing:
        print(f"  - {n}")

print("Done. Restart the app (python3 app.py) and hard-refresh the browser.")
