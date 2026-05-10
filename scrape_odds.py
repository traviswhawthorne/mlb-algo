#!/usr/bin/env python3
"""
Scrape historical SBR odds for missing 2025 dates (Apr 1 – Jul 30).
Run in background; caches each date to cache/historical_odds/.
"""
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from historical_odds_fetcher import get_historical_odds, CACHE_DIR

start = date(2025, 4, 1)
end   = date(2025, 7, 30)

dates = []
cur = start
while cur <= end:
    dates.append(str(cur))
    cur += timedelta(days=1)

missing = [d for d in dates if not (CACHE_DIR / f"{d}.json").exists()]
print(f"Dates to scrape: {len(missing)} of {len(dates)} (rest cached)")

for i, d in enumerate(missing, 1):
    print(f"[{i}/{len(missing)}] {d} ...", flush=True)
    try:
        result = get_historical_odds(d)
        n = len(result) if result else 0
        print(f"  → {n} games", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

print("Done.")
