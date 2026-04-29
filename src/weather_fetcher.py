"""
Weather Fetcher
===============
Fetches game-time weather conditions from wttr.in (completely free, no API key).
Used to adjust expected run totals for temperature and wind.

Effect on the model:
  - Cold weather (< 50°F): ball doesn't carry as far, fewer runs
  - Hot weather (> 85°F): ball carries further, more runs
  - High wind: generally inflates offense slightly
  - Domed stadiums: weather irrelevant, run_factor = 1.0
"""

import requests
from datetime import date, datetime, timezone
import os
import json
import math

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Venue → "City,State" for wttr.in lookup
VENUE_CITIES = {
    "Coors Field":                "Denver,CO",
    "Great American Ball Park":   "Cincinnati,OH",
    "Globe Life Field":           "Arlington,TX",
    "Fenway Park":                "Boston,MA",
    "Wrigley Field":              "Chicago,IL",
    "Guaranteed Rate Field":      "Chicago,IL",
    "Yankee Stadium":             "New York,NY",
    "Citi Field":                 "Flushing,NY",
    "Truist Park":                "Cumberland,GA",
    "Kauffman Stadium":           "Kansas City,MO",
    "Camden Yards":               "Baltimore,MD",
    "Citizens Bank Park":         "Philadelphia,PA",
    "Busch Stadium":              "St. Louis,MO",
    "Minute Maid Park":           "Houston,TX",
    "Chase Field":                "Phoenix,AZ",
    "PNC Park":                   "Pittsburgh,PA",
    "T-Mobile Park":              "Seattle,WA",
    "Dodger Stadium":             "Los Angeles,CA",
    "Oracle Park":                "San Francisco,CA",
    "Petco Park":                 "San Diego,CA",
    "loanDepot park":             "Miami,FL",
    "Target Field":               "Minneapolis,MN",
    "Progressive Field":          "Cleveland,OH",
    "Comerica Park":              "Detroit,MI",
    "Nationals Park":             "Washington,DC",
    "Angel Stadium":              "Anaheim,CA",
    "American Family Field":      "Milwaukee,WI",
    "Oakland Coliseum":           "Oakland,CA",
    "Sutter Health Park":         "Sacramento,CA",
}

# Parks where weather is irrelevant (fixed domes or retractable roofs that are
# almost always closed due to extreme heat)
DOME_PARKS = {
    "Tropicana Field",    # Tampa Bay  — fixed dome
    "Rogers Centre",      # Toronto    — retractable, almost always closed
    "Minute Maid Park",   # Houston    — retractable, almost always closed (heat)
    "Chase Field",        # Phoenix    — retractable, almost always closed (heat)
}

# Direction from home plate toward center field, in degrees (0=N, 90=E, 180=S, 270=W).
# Wind blowing IN THIS direction is "blowing out" — favors offense.
# Wind blowing the opposite direction is "blowing in" — suppresses offense.
PARK_ORIENTATIONS = {
    "Fenway Park":               335,   # CF to NNW
    "Wrigley Field":              45,   # CF to NE (lake winds blow IN from NE)
    "Yankee Stadium":            315,   # CF to NW
    "Citi Field":                310,   # CF to WNW
    "Camden Yards":               10,   # CF nearly due N
    "Citizens Bank Park":         50,   # CF to NE
    "Great American Ball Park":  355,   # CF nearly due N
    "Busch Stadium":              40,   # CF to NNE
    "PNC Park":                  350,   # CF nearly due N
    "Dodger Stadium":            335,   # CF to NNW
    "Oracle Park":                60,   # CF to ENE (Bay winds blow in)
    "Petco Park":                315,   # CF to NW
    "Target Field":               10,   # CF nearly due N
    "Progressive Field":         350,   # CF nearly due N
    "Comerica Park":             350,   # CF nearly due N
    "Nationals Park":             15,   # CF to NNE
    "Angel Stadium":             335,   # CF to NNW
    "American Family Field":      45,   # CF to NE
    "Kauffman Stadium":          340,   # CF to NNW
    "Truist Park":                30,   # CF to NNE
    "loanDepot park":             45,   # CF to NE
    "Guaranteed Rate Field":     335,   # CF to NNW
    "Sutter Health Park":        335,   # CF to NNW
    "Oakland Coliseum":          310,   # CF to WNW
    "Coors Field":               355,   # CF nearly due N
    "Globe Life Field":          335,   # retractable, often open in Arlington
    "T-Mobile Park":             335,   # retractable, often open in Seattle
}


def get_weather(venue, game_time_utc=None):
    """
    Fetch weather for a given venue and return a run environment factor.

    Returns a dict:
      {
        "run_factor":   float (1.0 = neutral),
        "description":  str   (human-readable, shown in Excel),
        "dome":         bool,
        "temp_f":       float or None,
        "wind_mph":     float or None,
      }
    Returns None on any failure (model falls back to 1.0).
    """
    # ---- Dome check ----
    for dome in DOME_PARKS:
        if dome.lower() in venue.lower():
            return {
                "run_factor":  1.0,
                "description": "Dome",
                "dome":        True,
                "temp_f":      None,
                "wind_mph":    None,
            }

    # ---- City lookup ----
    city = None
    venue_lower = venue.lower()
    for park, city_val in VENUE_CITIES.items():
        if park.lower() in venue_lower or venue_lower in park.lower():
            city = city_val
            break

    if not city:
        return None

    # ---- Cache per city per game hour (not just per day) ----
    # Two games in the same city can have very different temps (day game vs night game)
    safe_city  = city.replace(",", "_").replace(" ", "_")
    game_hour  = _utc_to_local_hour(game_time_utc, city)
    cache_file = os.path.join(CACHE_DIR,
                              f"weather_{safe_city}_{date.today()}_{game_hour}h.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    # ---- Fetch from wttr.in ----
    try:
        resp = requests.get(
            f"https://wttr.in/{city}?format=j1",
            timeout=10,
            headers={"User-Agent": "MLB-betting-model/1.0"}
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return None

    # ---- Extract conditions at game-time local hour ----
    temp_f, wind_mph, wind_from_deg = _conditions_at_game_time(data, game_time_utc, city)

    if temp_f is None:
        return None

    # Look up park orientation for direction-aware wind calculation
    park_cf_deg = None
    for park, deg in PARK_ORIENTATIONS.items():
        if park.lower() in venue.lower() or venue.lower() in park.lower():
            park_cf_deg = deg
            break

    run_factor = _run_factor(temp_f, wind_mph, wind_from_deg, park_cf_deg)

    # Build wind description including direction
    if wind_from_deg is not None:
        compass = _deg_to_compass(wind_from_deg)
        wind_desc = f"{wind_mph:.0f} mph {compass}"
    else:
        wind_desc = f"{wind_mph:.0f} mph wind"

    result = {
        "run_factor":    run_factor,
        "description":   f"{temp_f:.0f}°F, {wind_desc}",
        "dome":          False,
        "temp_f":        temp_f,
        "wind_mph":      wind_mph,
        "wind_from_deg": wind_from_deg,
    }

    with open(cache_file, "w") as f:
        json.dump(result, f)

    return result


def _utc_to_local_hour(game_time_utc, city):
    """
    Convert a UTC game time string to the local hour at the venue city.
    Returns the local hour as an int (0-23), or 99 if conversion fails.

    wttr.in hourly forecasts use LOCAL time ("1900" = 7 PM local).
    We must compare against local hour, not UTC hour.
    """
    if not game_time_utc:
        return 99

    # Rough UTC offset by city (standard time offsets, DST handled via pytz when available)
    CITY_UTC_OFFSETS = {
        "Boston,MA":       -4,   # EDT
        "New York,NY":     -4,
        "Flushing,NY":     -4,
        "Philadelphia,PA": -4,
        "Baltimore,MD":    -4,
        "Washington,DC":   -4,
        "Pittsburgh,PA":   -4,
        "Cleveland,OH":    -4,
        "Detroit,MI":      -4,
        "Atlanta,GA":      -4,
        "Cumberland,GA":   -4,
        "Miami,FL":        -4,
        "Chicago,IL":      -5,   # CDT
        "St. Louis,MO":    -5,
        "Milwaukee,WI":    -5,
        "Kansas City,MO":  -5,
        "Minneapolis,MN":  -5,
        "Houston,TX":      -5,
        "Arlington,TX":    -5,
        "Cincinnati,OH":   -4,
        "Denver,CO":       -6,   # MDT
        "Phoenix,AZ":      -7,   # MST (no DST)
        "Seattle,WA":      -7,   # PDT
        "Oakland,CA":      -7,
        "Sacramento,CA":   -7,
        "San Francisco,CA":-7,
        "Los Angeles,CA":  -7,
        "Anaheim,CA":      -7,
        "San Diego,CA":    -7,
    }

    try:
        dt_utc = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
        offset = CITY_UTC_OFFSETS.get(city, -4)   # default EDT
        local_hour = (dt_utc.hour + offset) % 24
        return local_hour
    except Exception:
        return 99


def _conditions_at_game_time(data, game_time_utc, city=""):
    """
    Extract temperature, wind speed, and wind direction at game time.
    Returns (temp_f, wind_mph, wind_dir_deg) where wind_dir_deg is the
    meteorological "wind FROM" direction (0=N, 90=E, 180=S, 270=W).
    Falls back to current conditions if forecast parsing fails.
    """
    def _extract(h):
        temp  = float(h.get("tempF") or h.get("FeelsLikeF") or 72)
        speed = float(h.get("windspeedMiles") or 5)
        deg   = h.get("winddirDegree")
        wdir  = float(deg) if deg is not None else None
        return temp, speed, wdir

    try:
        if game_time_utc:
            target_local_hour = _utc_to_local_hour(game_time_utc, city)

            if target_local_hour != 99:
                best_h    = None
                best_diff = 99
                for day in data.get("weather", [])[:2]:
                    for h in day.get("hourly", []):
                        h_local = int(h.get("time", "0")) // 100
                        diff    = abs(h_local - target_local_hour)
                        diff    = min(diff, 24 - diff)
                        if diff < best_diff:
                            best_diff = diff
                            best_h    = h

                if best_h and best_diff <= 3:
                    return _extract(best_h)

        curr = data["current_condition"][0]
        return _extract(curr)

    except Exception:
        return None, None, None


def _deg_to_compass(deg):
    """Convert degrees to 8-point compass label, e.g. 45 → 'NE'."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


def _run_factor(temp_f, wind_mph, wind_from_deg=None, park_cf_deg=None):
    """
    Translate temperature and wind into a run environment multiplier.

    Temperature effect:
      Each 10°F above 72°F ≈ +0.5% more runs; below 72°F ≈ -1% per 10°F.

    Wind effect (direction-aware when park orientation is known):
      Most home runs are pulled to LF or RF, not straight to CF. We measure the
      wind component along three outfield axes — LF (CF−45°), CF, and RF (CF+45°)
      — and take the maximum. Any wind blowing into the outfield in any direction
      gets credit, not just wind aimed at dead center.

      wind_from_deg  : meteorological direction wind is coming FROM (wttr.in)
      park_cf_deg    : compass direction from home plate toward center field

      Blowing out: 0% under 10 mph | 2% at 10–15 | 6% at 16–20 | 10% over 20
      Blowing in: muted at 2% per 10 mph of component (capped at −8%)
      When direction is unknown, falls back to a neutral +1% per 10 mph above 10.
    """
    # --- Temperature ---
    #   < 50°F  → −4%  |  50–60°F → −2%  |  61–82°F → neutral
    #   83–93°F → +4%  |  94°F+   → +8%
    if temp_f >= 94:
        temp_factor = 1.08
    elif temp_f >= 83:
        temp_factor = 1.04
    elif temp_f >= 61:
        temp_factor = 1.00
    elif temp_f >= 50:
        temp_factor = 0.98
    else:
        temp_factor = 0.96

    # --- Wind ---
    if wind_from_deg is not None and park_cf_deg is not None:
        # Wind is blowing TOWARD: opposite of the "from" direction
        wind_toward_deg = (wind_from_deg + 180) % 360

        # Most home runs are pulled to LF or RF, not straight to CF.
        # Measure the wind component along LF (CF - 45°), CF, and RF (CF + 45°)
        # and take the maximum — any wind blowing into the outfield gets credit.
        best_component = max(
            wind_mph * math.cos(math.radians(
                (wind_toward_deg - (park_cf_deg + offset) + 180) % 360 - 180
            ))
            for offset in (-45, 0, 45)
        )

        # Blowing out: stepped thresholds (light wind has no effect)
        #   < 10 mph  →  0%
        #   10–15 mph →  2%
        #   16–20 mph →  6%  (+4%)
        #   > 20 mph  → 10%  (+4%)
        # Blowing in: muted at 2% per 10 mph of component
        if best_component <= 0:
            wind_effect = max(-0.08, best_component * 0.002)
        elif best_component < 10:
            wind_effect = 0.0
        elif best_component <= 15:
            wind_effect = 0.02
        elif best_component <= 20:
            wind_effect = 0.06
        else:
            wind_effect = 0.10
        wind_factor = 1.0 + wind_effect
    else:
        # No direction data — small generic boost for high wind
        wind_pts     = [0,    10,   20,   30,   40  ]
        wind_factors = [1.00, 1.00, 1.01, 1.02, 1.03]
        wind_factor  = _interp(wind_mph, wind_pts, wind_factors)

    return round(temp_factor * wind_factor, 4)


def _interp(x, xs, ys):
    """Simple piecewise linear interpolation (avoids numpy dependency here)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]
