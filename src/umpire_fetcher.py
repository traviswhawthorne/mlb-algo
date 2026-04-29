"""
Umpire Fetcher
==============
Computes HP umpire run-environment tendencies from this season's completed games.
No external API key required — uses only the free MLB Stats API.

A home plate umpire significantly affects the run environment:
  - Tight strike zone → more walks, fewer strikeouts, more runs
  - Wide strike zone  → fewer walks, more strikeouts, fewer runs

We measure each umpire's run_factor: runs per game relative to league average.
  > 1.0 = more runs than average (offense-friendly)
  < 1.0 = fewer runs than average (pitcher-friendly)
"""

import os
import json
import requests
from datetime import date, timedelta

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(CACHE_DIR, exist_ok=True)

MLB_API = "https://statsapi.mlb.com/api/v1"

MIN_GAMES     = 10     # current-season games needed to fully trust current data
MIN_GAMES_PRIOR = 20  # prior-season games needed to use prior data as fallback
MAX_FACTOR    = 1.10   # cap adjustment at +10%
MIN_FACTOR    = 0.90   # cap adjustment at -10%
FLAG_THRESHOLD = 0.03  # only print if factor differs from 1.0 by this much


def _fetch_season_tendencies(season):
    """
    Fetch and compute umpire run factors for a single season.
    Returns {umpire_name: {"run_factor": float, "games": int}}, no minimum applied.
    Cached once per day for current season, permanently for prior seasons.
    """
    today = date.today()
    is_current = (season == today.year)
    if is_current:
        cache_file = os.path.join(CACHE_DIR, f"umpire_tendencies_{season}_{today}.json")
    else:
        cache_file = os.path.join(CACHE_DIR, f"umpire_tendencies_{season}_full.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    start_date = f"{season}-03-20"
    end_date   = (today - timedelta(days=1)).strftime("%Y-%m-%d") if is_current \
                 else f"{season}-11-30"

    url = f"{MLB_API}/schedule"
    params = {
        "sportId":   1,
        "startDate": start_date,
        "endDate":   end_date,
        "gameType":  "R",
        "hydrate":   "officials,linescore",
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Umpire fetch failed for {season} — {e}")
        return {}

    umpire_runs = {}
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            hp_name = None
            for official in game.get("officials", []):
                if official.get("officialType") == "Home Plate":
                    hp_name = official.get("official", {}).get("fullName")
                    break
            if not hp_name:
                continue
            ls    = game.get("linescore", {})
            teams = ls.get("teams", {})
            try:
                runs = (int(teams.get("home", {}).get("runs", 0)) +
                        int(teams.get("away", {}).get("runs", 0)))
            except (TypeError, ValueError):
                continue
            umpire_runs.setdefault(hp_name, []).append(runs)

    if not umpire_runs:
        with open(cache_file, "w") as f:
            json.dump({}, f)
        return {}

    all_runs   = [r for runs in umpire_runs.values() for r in runs]
    league_avg = sum(all_runs) / len(all_runs) if all_runs else 9.0

    result = {}
    for name, runs_list in umpire_runs.items():
        games = len(runs_list)
        avg        = sum(runs_list) / games
        run_factor = round(avg / league_avg, 4)
        result[name] = {"run_factor": run_factor, "games": games}

    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)

    return result


def get_umpire_tendencies(season):
    """
    Return umpire run factors for the given season, blended with prior-year data.

    Blending logic per umpire:
      - Current season >= MIN_GAMES:  use current only
      - Current season < MIN_GAMES:   blend with prior year, weighting toward prior
        when current sample is small (0 games → 100% prior, MIN_GAMES → 100% current)
      - No current data at all:        use prior year if >= MIN_GAMES_PRIOR games
      - No prior data either:          neutral (1.0), excluded from result

    Cached once per day.
    """
    cache_file = os.path.join(CACHE_DIR,
                              f"umpire_tendencies_blended_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print("  Computing umpire tendencies (current + prior year) ...")

    current = _fetch_season_tendencies(season)
    prior   = _fetch_season_tendencies(season - 1)

    all_names = set(current) | set(prior)
    result = {}

    for name in all_names:
        cur_data  = current.get(name)
        pri_data  = prior.get(name)

        cur_games  = cur_data["games"]  if cur_data  else 0
        cur_factor = cur_data["run_factor"] if cur_data else 1.0
        pri_games  = pri_data["games"]  if pri_data  else 0
        pri_factor = pri_data["run_factor"] if pri_data else 1.0

        if cur_games >= MIN_GAMES:
            # Enough current data — use it directly
            blended = cur_factor
            source  = "current"
        elif cur_games > 0 and pri_games >= MIN_GAMES_PRIOR:
            # Blend: weight toward prior when current sample is small
            w_cur  = cur_games / MIN_GAMES
            w_pri  = 1.0 - w_cur
            blended = round(w_cur * cur_factor + w_pri * pri_factor, 4)
            source  = f"blended ({cur_games}cur/{pri_games}prior)"
        elif pri_games >= MIN_GAMES_PRIOR:
            # No current data — fall back to prior year entirely
            blended = pri_factor
            source  = f"prior only ({pri_games}g)"
        else:
            # Not enough data either year
            continue

        result[name] = {
            "run_factor": round(max(MIN_FACTOR, min(blended, MAX_FACTOR)), 4),
            "games":      cur_games,
            "source":     source,
        }

    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)

    cur_count  = sum(1 for v in result.values() if "current" in v["source"])
    pri_count  = sum(1 for v in result.values() if "prior" in v["source"])
    print(f"  Umpire tendencies: {len(result)} umpires  "
          f"| {cur_count} current-season | {pri_count} prior-year fallback")
    return result


def get_game_umpire(officials_list):
    """
    Extract the HP umpire's full name from a game's officials list.
    Returns None if not found.
    """
    for official in (officials_list or []):
        if official.get("officialType") == "Home Plate":
            return official.get("official", {}).get("fullName")
    return None


def get_umpire_run_factor(umpire_name, tendencies_dict):
    """
    Return the run-environment multiplier for a given HP umpire.
    Returns 1.0 (neutral) for unknown umpires or empty data.
    Capped to [MIN_FACTOR, MAX_FACTOR] to avoid overreacting.
    """
    if not umpire_name or not tendencies_dict:
        return 1.0

    data = tendencies_dict.get(umpire_name)
    if not data:
        return 1.0

    factor = data["run_factor"]
    return max(MIN_FACTOR, min(factor, MAX_FACTOR))
