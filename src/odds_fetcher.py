"""
Odds Fetcher
============
Pulls today's MLB odds from The Odds API (free tier).

Free tier: 500 requests/month
One call per day pulls moneylines, run lines, AND totals simultaneously.
That's ~180 calls for a full MLB season — well within the free limit.

Get your free key at: https://the-odds-api.com
"""

import requests
from datetime import datetime, timezone, timedelta


ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"


def _merge_events(list_a, list_b):
    """Merge two Odds API event lists by game id, combining their bookmaker markets."""
    if not list_a:
        return list_b or []
    if not list_b:
        return list_a

    by_id = {ev["id"]: ev for ev in list_a}
    for event in list_b:
        base = by_id.get(event["id"])
        if not base:
            by_id[event["id"]] = event
            continue
        core_bm_keys = {bm["key"] for bm in base.get("bookmakers", [])}
        for bm in event.get("bookmakers", []):
            if bm["key"] not in core_bm_keys:
                base.setdefault("bookmakers", []).append(bm)
            else:
                for base_bm in base["bookmakers"]:
                    if base_bm["key"] == bm["key"]:
                        existing = {m["key"] for m in base_bm.get("markets", [])}
                        for mkt in bm.get("markets", []):
                            if mkt["key"] not in existing:
                                base_bm.setdefault("markets", []).append(mkt)
    return list(by_id.values())


def _median_odds(prices):
    """Return the median American odds from a list, rounded to nearest integer."""
    # Valid American odds are <= -100 or >= 100; anything in (-99, 99) is a bad value
    valid = [p for p in prices if p is not None and (p >= 100 or p <= -100)]
    if not valid:
        return None
    s = sorted(valid)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return round(s[mid])
    return round((s[mid - 1] + s[mid]) / 2)


def _consensus_line(lines):
    """
    Return the consensus total line from a list of bookmaker lines.
    Uses the mode (most common value). On ties, returns the most common
    standard half-point line (e.g. 8.5, 9.0, 9.5) by preferring .0 or .5.
    Falls back to median if no clear mode.
    """
    if not lines:
        return None
    counts = {}
    for v in lines:
        counts[v] = counts.get(v, 0) + 1
    max_count = max(counts.values())
    candidates = [v for v, c in counts.items() if c == max_count]
    # Among tied candidates prefer standard .0 / .5 lines
    standard = [v for v in candidates if v % 0.5 == 0]
    pool = standard if standard else candidates
    return sorted(pool)[len(pool) // 2]   # median of candidates


def get_mlb_odds(api_key):
    """
    Fetch today's MLB odds from The Odds API.

    Makes up to three requests:
      1. Core markets (h2h, spreads, totals)  — always attempted, required for bets
      2. F5 markets (h2h_h1, spreads_h1, totals_h1) — skipped if unavailable
      3. K props (pitcher_strikeouts)          — skipped if unavailable

    Returns a merged list of game objects (one per game, all markets combined),
    or [] on failure.
    """
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("  No Odds API key set. Skipping odds comparison.")
        print("  Get a free key at: https://the-odds-api.com")
        return []

    # Restrict to today's calendar date in Eastern Time.
    # Midnight ET = 04:00 UTC (EDT, April–October).
    # Window: today 04:00 UTC → tomorrow 04:00 UTC covers every MLB game today
    # and excludes tomorrow's slate (earliest first pitch ~5 PM UTC tomorrow).
    now_utc       = datetime.now(timezone.utc)
    # Shift to ET to find "today's" date (EDT = UTC-4 during the season)
    now_et        = now_utc - timedelta(hours=4)
    et_date       = now_et.date()
    commence_from = datetime(et_date.year, et_date.month, et_date.day,
                             4, 0, 0, tzinfo=timezone.utc)
    commence_to   = commence_from + timedelta(hours=24)
    cf_str        = commence_from.strftime("%Y-%m-%dT%H:%M:%SZ")
    ct_str        = commence_to.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fetch(markets, label):
        params = {
            "apiKey":            api_key,
            "regions":           "us",
            "markets":           markets,
            "oddsFormat":        "american",
            "commenceTimeFrom":  cf_str,
            "commenceTimeTo":    ct_str,
        }
        try:
            resp = requests.get(ODDS_API_URL, params=params, timeout=15)
            resp.raise_for_status()
            remaining = resp.headers.get("x-requests-remaining", "?")
            used      = resp.headers.get("x-requests-used",      "?")
            print(f"  Odds API [{label}] — used: {used} | remaining: {remaining}")
            return resp.json()
        except requests.exceptions.HTTPError as e:
            # e.response can be None in some environments even when status is set
            code = e.response.status_code if e.response else 0
            if code == 0:
                msg = str(e)
                if "422" in msg: code = 422
                elif "401" in msg: code = 401
                elif "429" in msg: code = 429
            if code == 401:
                print("  ERROR: Invalid Odds API key — check config.py")
            elif code == 422:
                return None   # market unavailable — caller decides whether to warn
            elif code == 429:
                print("  ERROR: Odds API monthly request limit reached")
            else:
                print(f"  WARNING: Odds API [{label}] failed ({code}) — {e}")
            return []
        except Exception as e:
            print(f"  WARNING: Odds API [{label}] failed — {e}")
            return []

    # Request 1: core markets — always needed
    core = _fetch("h2h,spreads,totals", "core")
    if not core:
        return []

    # Request 2: F5 markets
    f5 = _fetch("h2h_h1,spreads_h1,totals_h1", "F5")
    if f5 is None:
        print("  Odds API [F5] — 422 returned (market key rejected or unavailable)")
        f5 = []
    elif f5 == []:
        print("  Odds API [F5] — returned 0 events (no F5 lines posted yet today)")
    else:
        f5_with_markets = sum(1 for e in f5 if any(
            m["key"] in ("h2h_h1","spreads_h1","totals_h1")
            for bm in e.get("bookmakers",[]) for m in bm.get("markets",[])))
        print(f"  Odds API [F5] — {len(f5)} events, {f5_with_markets} with F5 markets")

    # Request 3: K props
    kprops = _fetch("pitcher_strikeouts", "K-props")
    if kprops is None:
        kprops = _fetch("player_strikeouts", "K-props (alt)")
    if kprops is None:
        print("  Odds API [K-props] — 422 returned (market key rejected or unavailable)")
        kprops = []
    elif kprops == []:
        print("  Odds API [K-props] — returned 0 events (no K lines posted yet today)")
    else:
        print(f"  Odds API [K-props] — {len(kprops)} events returned")

    expanded = _merge_events(f5, kprops) if (f5 or kprops) else []
    return _merge_events(core, expanded)


def parse_odds(odds_data):
    """
    Parse raw Odds API response into a clean dict keyed by matchup string.

    Returns:
      {
        "Boston Red Sox @ New York Yankees": {
          "away_team": "Boston Red Sox",
          "home_team": "New York Yankees",
          "moneyline": {"away": +150, "home": -170},
          "runline":   {"away": +130, "home": -150},
          "total":     {"over": -110, "under": -110, "line": 8.5},
          "commence_str": "2024-04-15T18:05:00Z",
        },
        ...
      }
    We take the median odds across all bookmakers for each side.

    Doubleheader handling: each Odds API event has a unique ID, so game 1 and
    game 2 of a doubleheader are kept in separate collectors (keyed by event ID)
    and never merged.  The output dict uses "Away @ Home" for the first game and
    "Away @ Home (2)" for the second, sorted by commence time.  match_odds_game
    picks the right entry using the MLB game start time.
    """
    # ---- Step 1: accumulate raw values per event (keyed by event ID) ----
    collectors = {}   # event_id → dict of raw value lists
    now_utc = datetime.now(timezone.utc)

    for event in odds_data:
        # Skip games that have already started — their odds are live/in-game
        commence_str = event.get("commence_time", "")
        if commence_str:
            try:
                commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                if commence_dt <= now_utc:
                    continue
            except ValueError:
                pass

        away_team = event.get("away_team", "")
        home_team = event.get("home_team", "")
        # Use the Odds API event ID as the collector key so that doubleheader
        # games (same teams, different start times) are never merged together.
        event_id = event.get("id") or f"{away_team} @ {home_team} @ {commence_str}"

        if event_id not in collectors:
            collectors[event_id] = {
                "away_team":    away_team,
                "home_team":    home_team,
                "commence_str": commence_str,
                "ml_away": [], "ml_home": [],
                # Run line prices split by direction so we never mix -1.5 and +1.5 odds
                "rl_away_minus": [], "rl_away_plus": [],   # away at -1.5 vs +1.5
                "rl_home_minus": [], "rl_home_plus": [],   # home at -1.5 vs +1.5
                "rl_away_pts": [], "rl_home_pts": [],
                "tot_over": [], "tot_under": [], "tot_line": [],
                "f5_ml_away": [], "f5_ml_home": [],
                "f5_rl_away": [], "f5_rl_home": [],
                "f5_over": [], "f5_under": [], "f5_line": [],
                "k_props": {},
            }
        c = collectors[event_id]

        for bm in event.get("bookmakers", []):
            for market in bm.get("markets", []):
                mkey     = market["key"]
                outcomes = market.get("outcomes", [])

                if mkey == "h2h":
                    for o in outcomes:
                        if o["name"] == away_team:   c["ml_away"].append(o["price"])
                        elif o["name"] == home_team: c["ml_home"].append(o["price"])

                elif mkey == "spreads":
                    for o in outcomes:
                        pt = o.get("point")
                        if pt is None or abs(float(pt)) != 1.5:
                            continue   # skip alternate lines (±0.5, ±2.5, etc.)
                        pt = float(pt)
                        if o["name"] == away_team:
                            (c["rl_away_minus"] if pt < 0 else c["rl_away_plus"]).append(o["price"])
                            c["rl_away_pts"].append(pt)
                        elif o["name"] == home_team:
                            (c["rl_home_minus"] if pt < 0 else c["rl_home_plus"]).append(o["price"])
                            c["rl_home_pts"].append(pt)

                elif mkey == "totals":
                    for o in outcomes:
                        if o["name"] == "Over":
                            c["tot_over"].append(o["price"])
                            if "point" in o: c["tot_line"].append(o["point"])
                        elif o["name"] == "Under":
                            c["tot_under"].append(o["price"])

                elif mkey == "h2h_h1":
                    for o in outcomes:
                        if o["name"] == away_team:   c["f5_ml_away"].append(o["price"])
                        elif o["name"] == home_team: c["f5_ml_home"].append(o["price"])

                elif mkey == "spreads_h1":
                    for o in outcomes:
                        if o["name"] == away_team:   c["f5_rl_away"].append(o["price"])
                        elif o["name"] == home_team: c["f5_rl_home"].append(o["price"])

                elif mkey == "totals_h1":
                    for o in outcomes:
                        if o["name"] == "Over":
                            c["f5_over"].append(o["price"])
                            if "point" in o: c["f5_line"].append(o["point"])
                        elif o["name"] == "Under":
                            c["f5_under"].append(o["price"])

                elif mkey in ("pitcher_strikeouts", "player_strikeouts"):
                    for o in outcomes:
                        pname = o.get("description", "")
                        side  = o.get("name", "")
                        price = o.get("price")
                        line  = o.get("point")
                        if not pname or not side or price is None:
                            continue
                        entry = c["k_props"].setdefault(
                            pname, {"over": None, "under": None, "line": None}
                        )
                        if side == "Over":
                            if entry["over"] is None or price > entry["over"]:
                                entry["over"] = price
                            if line is not None:
                                entry["line"] = line
                        elif side == "Under":
                            if entry["under"] is None or price > entry["under"]:
                                entry["under"] = price

    # ---- Step 2: compute final values from accumulated data ----
    # Group by matchup so we can detect doubleheaders and assign disambiguated keys.
    matchup_groups = {}   # "Away @ Home" → list of game_odds dicts (one per event)

    for _event_id, c in collectors.items():
        away_team = c["away_team"]
        home_team = c["home_team"]

        game_odds = {
            "away_team":    away_team,
            "home_team":    home_team,
            "commence_str": c.get("commence_str", ""),
            "moneyline":    {"away": None, "home": None},
            "runline":      {"away": None, "home": None, "away_point": None, "home_point": None},
            "total":        {"over": None, "under": None, "line": None},
            "f5_moneyline": {"away": None, "home": None},
            "f5_runline":   {"away": None, "home": None},
            "f5_total":     {"over": None, "under": None, "line": None},
            "k_props":      c["k_props"],
        }

        if c["ml_away"]:     game_odds["moneyline"]["away"]       = _median_odds(c["ml_away"])
        if c["ml_home"]:     game_odds["moneyline"]["home"]       = _median_odds(c["ml_home"])

        # Run line: use the moneyline to determine which team is the -1.5 favorite.
        # ML is always unambiguous; counting spread entries can flip when books post
        # alternate ±1.5 lines alongside the standard line in the same market.
        ml_away_val = _median_odds(c["ml_away"]) if c["ml_away"] else None
        ml_home_val = _median_odds(c["ml_home"]) if c["ml_home"] else None
        if ml_away_val is not None and ml_home_val is not None:
            away_is_minus = ml_away_val < ml_home_val  # lower (more negative) ML = favorite
        else:
            away_is_minus = len(c["rl_away_minus"]) >= len(c["rl_away_plus"])
        if away_is_minus:
            rl_away_prices = c["rl_away_minus"]
            rl_home_prices = c["rl_home_plus"]
            away_pt, home_pt = -1.5, 1.5
        else:
            rl_away_prices = c["rl_away_plus"]
            rl_home_prices = c["rl_home_minus"]
            away_pt, home_pt = 1.5, -1.5

        if rl_away_prices: game_odds["runline"]["away"]       = _median_odds(rl_away_prices)
        if rl_home_prices: game_odds["runline"]["home"]       = _median_odds(rl_home_prices)
        if rl_away_prices: game_odds["runline"]["away_point"] = away_pt
        if rl_home_prices: game_odds["runline"]["home_point"] = home_pt
        if c["tot_over"]:    game_odds["total"]["over"]       = _median_odds(c["tot_over"])
        if c["tot_under"]:   game_odds["total"]["under"]      = _median_odds(c["tot_under"])
        if c["tot_line"]:    game_odds["total"]["line"]       = _consensus_line(c["tot_line"])

        if c["f5_ml_away"]:  game_odds["f5_moneyline"]["away"]  = _median_odds(c["f5_ml_away"])
        if c["f5_ml_home"]:  game_odds["f5_moneyline"]["home"]  = _median_odds(c["f5_ml_home"])
        if c["f5_rl_away"]:  game_odds["f5_runline"]["away"]    = _median_odds(c["f5_rl_away"])
        if c["f5_rl_home"]:  game_odds["f5_runline"]["home"]    = _median_odds(c["f5_rl_home"])
        if c["f5_over"]:     game_odds["f5_total"]["over"]      = _median_odds(c["f5_over"])
        if c["f5_under"]:    game_odds["f5_total"]["under"]     = _median_odds(c["f5_under"])
        if c["f5_line"]:     game_odds["f5_total"]["line"]      = _consensus_line(c["f5_line"])

        matchup = f"{away_team} @ {home_team}"
        matchup_groups.setdefault(matchup, []).append(game_odds)

    # Build the final dict.  For doubleheaders, sort events by commence time and
    # assign "Away @ Home" to game 1 and "Away @ Home (2)" to game 2, etc.
    games = {}
    for matchup, entries in matchup_groups.items():
        entries.sort(key=lambda e: e.get("commence_str", ""))
        for i, entry in enumerate(entries):
            final_key = matchup if i == 0 else f"{matchup} ({i + 1})"
            games[final_key] = entry

    return games


def identify_bookmakers(api_key):
    """
    One-time helper: print every bookmaker's moneyline, run line, and total
    for all of today's upcoming games so you can match your sportsbook to
    an Odds API key.

    Usage:  from odds_fetcher import identify_bookmakers
            identify_bookmakers("YOUR_KEY")
    """
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("No API key set.")
        return

    now_utc = datetime.now(timezone.utc)
    et_date = (now_utc - timedelta(hours=4)).date()
    day_start = datetime(et_date.year, et_date.month, et_date.day, 4, 0, 0,
                         tzinfo=timezone.utc)
    params = {
        "apiKey":           api_key,
        "regions":          "us",
        "markets":          "h2h,spreads,totals",
        "oddsFormat":       "american",
        "commenceTimeFrom": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo":   (day_start + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        resp = requests.get(ODDS_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"Error: {e}")
        return

    seen_keys = set()
    print(f"\n{'BOOKMAKER':<22}  {'ML Away':>9}  {'ML Home':>9}  {'RL Away':>9}  {'RL Home':>9}  {'Total':>7}")
    print("-" * 80)

    for event in events:
        # Skip games that have already started
        commence = event.get("commence_time", "")
        try:
            ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if ct <= now_utc:
                continue
        except Exception:
            pass

        away = event.get("away_team", "")
        home = event.get("home_team", "")
        print(f"\n  {away} @ {home}  (starts {commence})")

        for bm in event.get("bookmakers", []):
            bk = bm.get("key", "?")

            ml_a = ml_h = rl_a = rl_h = tot = "—"
            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    name, price = o.get("name", ""), o.get("price", "")
                    if mkt["key"] == "h2h":
                        if name == away:  ml_a = f"{price:+d}"
                        elif name == home: ml_h = f"{price:+d}"
                    elif mkt["key"] == "spreads":
                        pt = o.get("point", "")
                        if name == away:  rl_a = f"{price:+d}({pt})"
                        elif name == home: rl_h = f"{price:+d}({pt})"
                    elif mkt["key"] == "totals" and name == "Over":
                        tot = f"{o.get('point','')} {price:+d}"

            print(f"    {bk:<20}  {ml_a:>9}  {ml_h:>9}  {rl_a:>12}  {rl_h:>12}  {tot:>10}")
        break   # just the first upcoming game — enough to identify your book

    print(f"\nTotal requests used: {resp.headers.get('x-requests-used','?')}")
