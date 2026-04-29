#!/usr/bin/env python3
"""
CSV Odds Loader
===============
Reads oddsData.csv (historical odds 2012-2021) and writes per-date JSON
cache files to cache/historical_odds/ in the same format as the SBR scraper.

Pairs visitor (V) and home (H) rows by (date, gameNumber, total, overOdds, underOdds)
fingerprint, then disambiguates multiple-game groups by sorting V ascending by ML
and H descending by ML so complementary lines pair naturally.

Usage (standalone):
    py src/csv_odds_loader.py [--force] [--year YYYY]

    --force      : overwrite existing cache files (default: skip)
    --year YYYY  : only process the specified year (e.g. --year 2021)
"""

import csv
import json
import sys
from pathlib import Path
from collections import defaultdict

# Support both "py src/csv_odds_loader.py" and "from src.csv_odds_loader import ..."
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from src.historical_odds_fetcher import SBR_TO_MLB, CACHE_DIR

CSV_PATH = _ROOT / "oddsData.csv"


def _team_full_name(abbrev, year):
    """Return full MLB API team name, with year-aware overrides."""
    if abbrev == "CLE" and year < 2022:
        return "Cleveland Indians"
    return SBR_TO_MLB.get(abbrev)


def _to_int(val):
    try:
        return int(float(val)) if val and str(val).strip() else None
    except (ValueError, TypeError):
        return None


def _to_float(val):
    try:
        return float(val) if val and str(val).strip() else None
    except (ValueError, TypeError):
        return None


def _pair_rows(v_rows, h_rows):
    """
    Pair away (V) and home (H) rows into game tuples.

    When multiple games share the same fingerprint, sort V ascending by ML
    and H descending by ML so the biggest away-favorite pairs with the biggest
    home-underdog (and vice versa), minimising cross-game vig imbalance.
    """
    if not v_rows or not h_rows:
        return []
    if len(v_rows) == 1 and len(h_rows) == 1:
        return [(v_rows[0], h_rows[0])]

    v_sorted = sorted(v_rows, key=lambda r: _to_int(r["line"]) or 0)
    h_sorted = sorted(h_rows, key=lambda r: -(_to_int(r["line"]) or 0))

    return list(zip(v_sorted, h_sorted))


def load_csv_to_cache(csv_path=None, force=False, year_filter=None):
    """
    Read oddsData.csv and write per-date JSON cache files.

    Args:
        csv_path:    Path to the CSV file (default: oddsData.csv in repo root)
        force:       If True, overwrite existing cache files
        year_filter: If set (int), only process rows for that year

    Returns:
        (written: list[str], skipped: list[str])
    """
    csv_path = Path(csv_path) if csv_path else CSV_PATH
    if not csv_path.exists():
        print(f"  ERROR: CSV not found at {csv_path}")
        return [], []

    # Group rows by (date, gameNumber, total, overOdds, underOdds)
    groups = defaultdict(lambda: {"V": [], "H": []})

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row["date"].strip()
            if year_filter and not date.startswith(str(year_filter)):
                continue
            key = (
                date,
                row["gameNumber"].strip(),
                row["total"].strip(),
                row["overOdds"].strip(),
                row["underOdds"].strip(),
            )
            at = row["at"].strip()
            if at in ("V", "H"):
                groups[key][at].append(row)

    # Build per-date odds dicts
    by_date = defaultdict(dict)
    mismatch_count = 0

    for (date, game_num, total, over_odds, under_odds), sides in groups.items():
        year       = int(date[:4])
        v_rows     = sides.get("V", [])
        h_rows     = sides.get("H", [])

        if v_rows and h_rows and len(v_rows) != len(h_rows):
            mismatch_count += 1

        for v, h in _pair_rows(v_rows, h_rows):
            away_abbr = v["team"].strip()
            home_abbr = h["team"].strip()

            away_full = _team_full_name(away_abbr, year)
            home_full = _team_full_name(home_abbr, year)

            if not away_full or not home_full:
                continue

            game_key = f"{away_full} @ {home_full}"

            by_date[date][game_key] = {
                "away_team": away_full,
                "home_team": home_full,
                "moneyline": {
                    "away": _to_int(v["line"]),
                    "home": _to_int(h["line"]),
                },
                "runline": {
                    "away":       _to_int(v["runLineOdds"]),
                    "home":       _to_int(h["runLineOdds"]),
                    "away_point": _to_float(v["runLine"]),
                    "home_point": _to_float(h["runLine"]),
                },
                "total": {
                    "over":  _to_int(over_odds),
                    "under": _to_int(under_odds),
                    "line":  _to_float(total),
                },
            }

    # Write cache files
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    skipped = []

    for date in sorted(by_date):
        cache_file = CACHE_DIR / f"{date}.json"
        if cache_file.exists() and not force:
            skipped.append(date)
            continue
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(by_date[date], f, indent=2)
        written.append(date)

    if mismatch_count:
        print(f"  Warning: {mismatch_count} fingerprint groups had unequal V/H counts (partial pairing used)")

    return written, skipped


if __name__ == "__main__":
    force       = "--force" in sys.argv
    year_filter = None
    if "--year" in sys.argv:
        idx = sys.argv.index("--year")
        if idx + 1 < len(sys.argv):
            try:
                year_filter = int(sys.argv[idx + 1])
            except ValueError:
                pass

    print()
    print("=" * 62)
    print("  CSV Odds Loader  —  oddsData.csv → cache/historical_odds/")
    print("=" * 62)
    print()
    print(f"  Source: {CSV_PATH}")
    if year_filter:
        print(f"  Filter: year = {year_filter}")
    if force:
        print("  Mode:   --force (overwriting existing cache files)")
    print()
    print("  Processing ...")

    written, skipped = load_csv_to_cache(force=force, year_filter=year_filter)

    print(f"  Written:  {len(written)} date files")
    print(f"  Skipped:  {len(skipped)} (already cached, use --force to overwrite)")
    if written:
        years = sorted(set(d[:4] for d in written))
        print(f"  Years:    {', '.join(years)}")
        print(f"  Dates:    {written[0]}  →  {written[-1]}")
    print()
    print("  Done. Run a backtest with --odds to use the cached data.")
    print("  Example: py backtest.py --odds 2021")
    print()
