"""
Games Fetcher
=============
Pulls today's MLB schedule and probable pitchers from the free MLB Stats API.
No API key required.
"""

import requests
from collections import defaultdict
from datetime import date, datetime, timedelta


MLB_API_BASE = "https://statsapi.mlb.com/api/v1"


def get_todays_games(game_date=None):
    """
    Pull today's MLB schedule with probable pitchers.

    Returns a list of dicts:
      {
        game_pk, game_time, away_team, home_team,
        away_pitcher, home_pitcher, venue
      }
    Probable pitchers will be None if not yet announced (TBD).
    """
    if game_date is None:
        game_date = date.today().strftime("%Y-%m-%d")

    url = f"{MLB_API_BASE}/schedule"
    params = {
        "sportId": 1,
        "date": game_date,
        "hydrate": "probablePitcher,team,venue,lineups,officials"
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Could not reach MLB Stats API: {e}")

    data = response.json()
    games = []

    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            # Include scheduled games and warmup (MLB API flips to "Live" ~30 min
            # before first pitch during warmup, before any runs are scored)
            status_obj    = game.get("status", {})
            abstract      = status_obj.get("abstractGameState", "")
            detailed      = status_obj.get("detailedState", "")

            if abstract == "Final":
                continue
            # Exclude games in progress. Warmup/Pre-Game are kept — the MLB API
            # flips to abstractGameState=Live ~30 min before first pitch during warmup.
            if abstract == "Live" and detailed not in ("Warmup", "Pre-Game"):
                continue

            game_type = game.get("gameType", "R")
            if game_type not in ("R", "P", "W", "F", "D", "L"):
                continue  # Skip spring training etc.

            teams = game.get("teams", {})
            away_team = teams.get("away", {}).get("team", {}).get("name", "Unknown")
            home_team = teams.get("home", {}).get("team", {}).get("name", "Unknown")

            # Probable pitchers (may be None if not announced)
            away_prob = teams.get("away", {}).get("probablePitcher")
            home_prob = teams.get("home", {}).get("probablePitcher")

            away_pitcher = away_prob.get("fullName") if away_prob else None
            home_pitcher = home_prob.get("fullName") if home_prob else None

            game_time = game.get("gameDate", "")
            venue = game.get("venue", {}).get("name", "Unknown")

            # Confirmed lineups (only available ~1-2 hours before first pitch)
            lineups = game.get("lineups", {})
            away_lineup_ids = [
                str(p["id"]) for p in lineups.get("awayPlayers", []) if p.get("id")
            ]
            home_lineup_ids = [
                str(p["id"]) for p in lineups.get("homePlayers", []) if p.get("id")
            ]

            games.append({
                "game_pk":         game.get("gamePk"),
                "game_time":       game_time,
                "away_team":       away_team,
                "home_team":       home_team,
                "away_pitcher":    away_pitcher,
                "home_pitcher":    home_pitcher,
                "venue":           venue,
                "away_lineup_ids": away_lineup_ids,
                "home_lineup_ids": home_lineup_ids,
                "officials":       game.get("officials", []),
            })

    # Fix doubleheader G2 placeholder times.
    # The MLB API often sets G2's gameDate to a few minutes after G1 (e.g. 9:40 AM
    # when G1 is 9:35 AM). Detect this by finding same-matchup pairs where G2 is
    # within 30 minutes of G1, and replace G2's time with G1 + 3.5 hours.
    matchup_groups = defaultdict(list)
    for g in games:
        matchup_groups[(g["away_team"], g["home_team"])].append(g)

    for pair in matchup_groups.values():
        if len(pair) != 2:
            continue
        pair.sort(key=lambda g: g["game_time"])
        g1, g2 = pair
        try:
            t1 = datetime.fromisoformat(g1["game_time"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(g2["game_time"].replace("Z", "+00:00"))
            if t2 - t1 < timedelta(minutes=30):
                estimated = t1 + timedelta(hours=3, minutes=30)
                g2["game_time"] = estimated.strftime("%Y-%m-%dT%H:%M:%SZ")
                print(f"  [games] DH G2 time corrected: {g2['away_team']} @ {g2['home_team']} "
                      f"→ {g2['game_time']} (est. G1+3.5h)")
        except Exception:
            pass

    return games
