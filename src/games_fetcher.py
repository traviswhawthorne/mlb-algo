"""
Games Fetcher
=============
Pulls today's MLB schedule and probable pitchers from the free MLB Stats API.
No API key required.
"""

import requests
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

            away_pitcher    = away_prob.get("fullName") if away_prob else None
            home_pitcher    = home_prob.get("fullName") if home_prob else None
            away_pitcher_id = away_prob.get("id")      if away_prob else None
            home_pitcher_id = home_prob.get("id")      if home_prob else None

            game_time    = game.get("gameDate", "")
            game_number  = game.get("gameNumber", 1)
            double_header = game.get("doubleHeader", "N")
            venue = game.get("venue", {}).get("name", "Unknown")

            # Confirmed lineups (only available ~1-2 hours before first pitch)
            lineups = game.get("lineups", {})
            away_lineup_ids = [
                str(p["id"]) for p in lineups.get("awayPlayers", []) if p.get("id")
            ]
            home_lineup_ids = [
                str(p["id"]) for p in lineups.get("homePlayers", []) if p.get("id")
            ]

            # For same-day traditional doubleheaders (doubleHeader="Y"), the MLB API
            # sets G2's gameDate to a placeholder a few minutes after G1. Correct it
            # to G1's listed time + 3.5 hours. This is applied per-game so it works
            # even after G1 has finished and been filtered out of the list.
            if double_header == "Y" and game_number == 2:
                try:
                    t = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
                    game_time = (t + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    print(f"  [games] DH G2 time estimated: {away_team} @ {home_team} → {game_time}")
                except Exception:
                    pass

            games.append({
                "game_pk":          game.get("gamePk"),
                "game_time":        game_time,
                "away_team":        away_team,
                "home_team":        home_team,
                "away_pitcher":     away_pitcher,
                "home_pitcher":     home_pitcher,
                "away_pitcher_id":  away_pitcher_id,
                "home_pitcher_id":  home_pitcher_id,
                "venue":            venue,
                "away_lineup_ids":  away_lineup_ids,
                "home_lineup_ids":  home_lineup_ids,
                "officials":        game.get("officials", []),
            })

    return games
