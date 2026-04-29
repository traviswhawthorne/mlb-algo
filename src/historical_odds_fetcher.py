"""
Historical Odds Fetcher
=======================
Scrapes closing lines from sportsbookreview.com for a given date.
Requires:  pip install playwright
           playwright install chromium

Usage:
    from src.historical_odds_fetcher import get_historical_odds
    odds = get_historical_odds("2025-07-31")
    # returns same structure as parse_odds() from odds_fetcher.py

Debug mode (dumps raw HTML to inspect page structure):
    odds = get_historical_odds("2025-07-31", debug=True)
"""

import json
import re
import sys
import asyncio
from pathlib import Path

# Force UTF-8 stdout so Unicode chars in print() don't crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CACHE_DIR = Path(__file__).parent.parent / "cache" / "historical_odds"

SBR_BASE = "https://www.sportsbookreview.com/betting-odds/mlb-baseball"

# SBR abbreviation → MLB Stats API full team name
SBR_TO_MLB = {
    "ARI": "Arizona Diamondbacks",
    "AZ":  "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox",
    "CHW": "Chicago White Sox",
    "SOX": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC":  "Kansas City Royals",
    "KCR": "Kansas City Royals",
    "KAN": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "OAK": "Oakland Athletics",
    "ATH": "Athletics",
    "SAC": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD":  "San Diego Padres",
    "SDP": "San Diego Padres",
    "SF":  "San Francisco Giants",
    "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TB":  "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WAS": "Washington Nationals",
    "WSH": "Washington Nationals",
    "WSN": "Washington Nationals",
}

# Valid SBR abbreviations (used to identify team name tokens in page text)
_ALL_ABBREVS = set(SBR_TO_MLB.keys())


# -----------------------------------------------------------------------
# Odds math helpers
# -----------------------------------------------------------------------

def _median_american(values):
    """Median of a list of American odds; rejects anything in (-99, 99)."""
    valid = [v for v in values if isinstance(v, int) and (v >= 100 or v <= -100)]
    if not valid:
        return None
    s = sorted(valid)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    avg = round((s[mid - 1] + s[mid]) / 2)
    # If averaging straddles 0 (e.g. -100 and +100), return the lower-abs value
    return avg if (avg >= 100 or avg <= -100) else s[mid - 1]


def _median_float(values):
    """Median of a list of floats."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _try_american(token):
    """Parse 'token' as American odds. Returns int or None."""
    m = re.match(r'^([+-])(\d{3,4})$', token.strip())
    if m:
        val = int(m.group(2))
        if m.group(1) == '-':
            val = -val
        if val >= 100 or val <= -100:
            return val
    return None


def _try_float_line(token):
    """Parse 'token' as a betting line (e.g. 8.5, -1.5). Returns float or None."""
    m = re.match(r'^([+-]?\d+\.5|[+-]?\d+\.0|[+-]?\d+)$', token.strip())
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            pass
    return None


# -----------------------------------------------------------------------
# Page text parser
# -----------------------------------------------------------------------

def _parse_page_text(text, market_type, debug=False):
    """
    Parse SBR page inner-text to extract game odds.

    SBR page structure per game:
      AWAY_ABBR
      (opener odds or '-')
      pitcher name
      rotation number
      HOME_ABBR
      (opener odds or '-')
      pitcher name
      rotation number
      (wager %)  (wager %)
      [book pairs alternating: away_value, home_value, away_value, home_value, ...]

    ML page: plain American odds  e.g. '+144', '-144'
    RL page: combined format       e.g. '+1.5-150', '-1.5+126'
    Totals:  combined line "O 9.5-104" / "U 9.5-109" as single text line

    After both teams are seen, book odds alternate: position 0 = away, 1 = home, 2 = away...
    Only parsed values (not dashes) increment the position counter.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    if debug:
        print(f"  [DEBUG] Page text (first 150 lines):")
        for i, l in enumerate(lines[:150]):
            print(f"    {i:3d}: {repr(l)}")

    # Regex patterns
    RE_RL_COMBINED  = re.compile(r'^([+-]\d+(?:\.\d+)?)([+-]\d{3,4})$')
    RE_TOT_COMBINED = re.compile(r'^([OU])\s*(\d+(?:\.\d+)?)([+-]\d{2,4})$')

    games = []
    state      = "init"   # init | away | both
    away_abbr  = None
    home_abbr  = None
    away_ints  = []
    away_floats = []
    home_ints  = []
    home_floats = []
    pair_idx   = 0    # alternating index: even=away, odd=home

    def _flush():
        nonlocal away_abbr, home_abbr, away_ints, away_floats
        nonlocal home_ints, home_floats, pair_idx
        if away_abbr and home_abbr:
            games.append({
                "away":        away_abbr,
                "home":        home_abbr,
                "away_ints":   away_ints[:],
                "away_floats": away_floats[:],
                "home_ints":   home_ints[:],
                "home_floats": home_floats[:],
            })
        away_abbr = home_abbr = None
        away_ints = away_floats = home_ints = home_floats = []
        pair_idx = 0

    RE_ABBREV_LIKE = re.compile(r'^[A-Z]{2,4}$')
    unknown_abbrevs = set()   # collect unrecognized potential abbreviations

    for line in lines:
        tokens = line.split()
        is_abbrev = (len(tokens) == 1 and tokens[0] in _ALL_ABBREVS)

        # Detect single-token all-uppercase strings that look like a team code
        # but aren't in our map — these cause cascade mismatches
        if (len(tokens) == 1 and RE_ABBREV_LIKE.match(tokens[0])
                and tokens[0] not in _ALL_ABBREVS):
            unknown_abbrevs.add(tokens[0])

        if is_abbrev:
            abbrev = tokens[0]
            if state == "init":
                away_abbr = abbrev
                away_ints, away_floats = [], []
                home_ints, home_floats = [], []
                pair_idx = 0
                state = "away"
            elif state == "away":
                home_abbr = abbrev
                pair_idx = 0
                state = "both"
            elif state == "both":
                _flush()
                away_abbr = abbrev
                away_ints, away_floats = [], []
                home_ints, home_floats = [], []
                pair_idx = 0
                state = "away"
            continue

        # Only parse odds once we've seen both teams
        if state != "both":
            continue

        # --- Totals: opener is the first O and first U seen ---
        m_tot = RE_TOT_COMBINED.match(line)
        if m_tot:
            side     = m_tot.group(1)
            line_val = float(m_tot.group(2))
            odds_val = int(m_tot.group(3))
            if abs(odds_val) >= 100:
                if side == 'O' and not away_ints:    # opener over only
                    away_ints.append(odds_val)
                    away_floats.append(line_val)
                elif side == 'U' and not home_ints:  # opener under only
                    home_ints.append(odds_val)
                    home_floats.append(line_val)
            pair_idx = 2   # lock out token loop so bare odds tokens don't bleed in
            continue

        # --- Token-by-token for RL combined or standard American ---
        # SBR column order: Opener | ProphetX | NoVig | [books...]
        # We want only the opener: pair_idx 0 = away, pair_idx 1 = home.
        # Dashes are NOT counted as column positions — a header dash between
        # the home-team name line and the odds grid would otherwise push the
        # opener into the wrong slot. ProphetX/NoVig real values still land at
        # pair_idx 2+ (after the opener pair) and are naturally ignored.
        for tok in tokens:
            if not tok or tok == '-':
                continue

            # RL combined: "+1.5-150" or "-1.5+126"
            m_rl = RE_RL_COMBINED.match(tok)
            if m_rl:
                pt       = float(m_rl.group(1))
                odds_val = int(m_rl.group(2))
                if abs(odds_val) >= 100 and abs(pt) <= 5.0:
                    if pair_idx == 0:
                        away_floats.append(pt)
                        away_ints.append(odds_val)
                    elif pair_idx == 1:
                        home_floats.append(pt)
                        home_ints.append(odds_val)
                pair_idx += 1
                continue

            # Standard American: "+144", "-144"
            ao = _try_american(tok)
            if ao is not None:
                if pair_idx == 0:
                    away_ints.append(ao)
                elif pair_idx == 1:
                    home_ints.append(ao)
                pair_idx += 1

    _flush()

    if debug:
        print(f"  [DEBUG] Parsed {len(games)} games from page text")
        if unknown_abbrevs:
            print(f"  [DEBUG] UNRECOGNIZED abbreviation candidates (add to SBR_TO_MLB): {sorted(unknown_abbrevs)}")

    return games


def _build_odds_from_parsed(ml_games, rl_games, total_games, debug=False):
    """
    Merge ML, RL, and totals parsed game lists into the standard odds dict.

    Returns dict keyed by "Away Full Name @ Home Full Name".
    """
    result = {}

    def _ensure(key, away_full, home_full):
        if key not in result:
            result[key] = {
                "away_team": away_full,
                "home_team": home_full,
                "moneyline": {"away": None, "home": None},
                "runline":   {"away": None, "home": None,
                              "away_point": None, "home_point": None},
                "total":     {"over": None, "under": None, "line": None},
            }

    # ---- ML ----
    for g in ml_games:
        away_full = SBR_TO_MLB.get(g["away"])
        home_full = SBR_TO_MLB.get(g["home"])
        if not away_full or not home_full:
            continue
        key = f"{away_full} @ {home_full}"
        _ensure(key, away_full, home_full)
        result[key]["moneyline"]["away"] = _median_american(g["away_ints"])
        result[key]["moneyline"]["home"] = _median_american(g["home_ints"])
        if debug:
            print(f"  ML  {g['away']}:{g['away_ints'][:5]}  {g['home']}:{g['home_ints'][:5]}")

    # ---- RL ----
    for g in rl_games:
        away_full = SBR_TO_MLB.get(g["away"])
        home_full = SBR_TO_MLB.get(g["home"])
        if not away_full or not home_full:
            continue
        key = f"{away_full} @ {home_full}"
        _ensure(key, away_full, home_full)
        r = result[key]["runline"]
        r["away"] = _median_american(g["away_ints"])
        r["home"] = _median_american(g["home_ints"])
        # The spread point: away team's RL point is the first float seen
        away_pts = g["away_floats"]
        home_pts = g["home_floats"]
        if away_pts:
            r["away_point"] = away_pts[0]
        if home_pts:
            r["home_point"] = home_pts[0]
        if debug:
            print(f"  RL  {g['away']}:{g['away_ints'][:3]}({away_pts[:1]})  "
                  f"{g['home']}:{g['home_ints'][:3]}({home_pts[:1]})")

    # ---- Totals ----
    for g in total_games:
        away_full = SBR_TO_MLB.get(g["away"])
        home_full = SBR_TO_MLB.get(g["home"])
        if not away_full or not home_full:
            continue
        key = f"{away_full} @ {home_full}"
        _ensure(key, away_full, home_full)
        t = result[key]["total"]
        # Over is on the away (top) row; under on home (bottom)
        t["over"]  = _median_american(g["away_ints"])
        t["under"] = _median_american(g["home_ints"])
        # Total line is a float like 8.5 — appears in both rows; take first
        all_floats = g["away_floats"] + g["home_floats"]
        if all_floats:
            t["line"] = _median_float(all_floats)
        if debug:
            print(f"  TOT {g['away']}:O{g['away_ints'][:3]}  "
                  f"{g['home']}:U{g['home_ints'][:3]}  line={all_floats[:2]}")

    return result


# -----------------------------------------------------------------------
# Playwright scraper
# -----------------------------------------------------------------------

async def _scrape_date_async(date_str, debug=False):
    """Scrape ML, RL, and totals for one date using Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ERROR: Playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    urls = {
        "ml":     f"{SBR_BASE}/?date={date_str}",
        "rl":     f"{SBR_BASE}/pointspread/full-game/?date={date_str}",
        "totals": f"{SBR_BASE}/totals/full-game/?date={date_str}",
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        context.set_default_timeout(60000)

        page_texts = {}

        for market, url in urls.items():
            page = await context.new_page()
            print(f"  Scraping {market.upper():6}  {url}")
            try:
                # networkidle waits until no network requests for 500ms —
                # gives the React app time to finish rendering data
                await page.goto(url, wait_until="networkidle", timeout=60000)

                # Extra wait: poll until the page has at least 5 American odds tokens
                # (guards against pages that load structure before filling in numbers)
                try:
                    await page.wait_for_function(
                        """() => {
                            const t = document.body.innerText;
                            const m = t.match(/[+-][0-9]{3}/g);
                            return m && m.length >= 5;
                        }""",
                        timeout=20000
                    )
                except Exception:
                    # Fall back: take whatever is there after a hard 3s pause
                    await page.wait_for_timeout(3000)
                    has_any = await page.evaluate(
                        "() => /[+-][0-9]{3}/.test(document.body.innerText)"
                    )
                    if not has_any:
                        print(f"  WARNING: no odds found on {market} page - skipping")

                text = await page.inner_text("body")
                page_texts[market] = text

                if debug:
                    debug_path = Path(__file__).parent.parent / f"debug_sbr_{market}_{date_str}.html"
                    html = await page.content()
                    debug_path.write_text(html, encoding="utf-8")
                    print(f"  [DEBUG] HTML saved -> {debug_path}")

            except Exception as e:
                print(f"  ERROR scraping {market}: {e}")
                page_texts[market] = ""
            finally:
                await page.close()

        await context.close()
        await browser.close()

    return page_texts


def _scrape_date(date_str, debug=False):
    """Synchronous wrapper around the async scraper."""
    return asyncio.run(_scrape_date_async(date_str, debug=debug))


# -----------------------------------------------------------------------
# Cache helpers
# -----------------------------------------------------------------------

def _cache_path(date_str):
    return CACHE_DIR / f"{date_str}.json"


def _load_cache(date_str):
    p = _cache_path(date_str)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(date_str, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(date_str), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------

def get_historical_odds(date_str, debug=False, force_refresh=False):
    """
    Get historical closing odds for all MLB games on a given date.

    Args:
        date_str:      ISO date string e.g. "2025-07-31"
        debug:         If True, dump raw HTML files and print parser trace
        force_refresh: If True, ignore cache and re-scrape

    Returns:
        dict keyed by "Away Team @ Home Team" (full MLB API names), each value:
        {
            "away_team": str,
            "home_team": str,
            "moneyline": {"away": int_or_None, "home": int_or_None},
            "runline":   {"away": int_or_None, "home": int_or_None,
                          "away_point": float_or_None, "home_point": float_or_None},
            "total":     {"over": int_or_None, "under": int_or_None,
                          "line": float_or_None},
        }
        Returns {} on failure.
    """
    if not force_refresh:
        cached = _load_cache(date_str)
        if cached is not None:
            return cached

    print(f"\n  Fetching SBR historical odds for {date_str} ...")
    page_texts = _scrape_date(date_str, debug=debug)
    if not page_texts:
        return {}

    ml_games     = _parse_page_text(page_texts.get("ml",     ""), "ml",     debug=debug)
    rl_games     = _parse_page_text(page_texts.get("rl",     ""), "rl",     debug=debug)
    total_games  = _parse_page_text(page_texts.get("totals", ""), "totals", debug=debug)

    odds = _build_odds_from_parsed(ml_games, rl_games, total_games, debug=debug)

    print(f"  Parsed {len(odds)} games from SBR")
    if odds:
        _save_cache(date_str, odds)
    else:
        print(f"  WARNING: No odds parsed for {date_str} — run with debug=True to inspect")

    return odds


def get_historical_odds_batch(date_list, debug=False):
    """
    Fetch historical odds for multiple dates.

    Args:
        date_list: list of ISO date strings
        debug:     enable debug output for each date

    Returns:
        dict keyed by date_str → odds dict (from get_historical_odds)
    """
    result = {}
    dates_to_fetch = [d for d in date_list if _load_cache(d) is None]
    dates_cached   = [d for d in date_list if _load_cache(d) is not None]

    if dates_cached:
        print(f"  {len(dates_cached)} dates already cached")
    if dates_to_fetch:
        print(f"  {len(dates_to_fetch)} dates need scraping")

    for d in dates_to_fetch:
        result[d] = get_historical_odds(d, debug=debug)

    for d in dates_cached:
        result[d] = _load_cache(d)

    return result


def find_odds_for_game(away_team, home_team, odds_dict):
    """
    Match a game to its entry in a historical odds dict.

    Tries exact match first, then last-word (city) matching.
    Returns the odds entry or None.
    """
    exact_key = f"{away_team} @ {home_team}"
    if exact_key in odds_dict:
        return odds_dict[exact_key]

    # Fuzzy: match by last token of each team name
    away_last = away_team.split()[-1].lower()
    home_last = home_team.split()[-1].lower()

    for key, val in odds_dict.items():
        k_away = val.get("away_team", "").split()[-1].lower()
        k_home = val.get("home_team", "").split()[-1].lower()
        if k_away == away_last and k_home == home_last:
            return val

    return None
