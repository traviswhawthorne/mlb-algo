"""
Name Matcher
============
Matches player/team names between MLB Stats API and FanGraphs.

The two data sources use slightly different name formats, so we do
fuzzy matching to find the right row in the stats data.
"""


# ---- Full team name -> FanGraphs abbreviation ----
TEAM_ABBREV = {
    "New York Yankees":       "NYY",
    "Boston Red Sox":         "BOS",
    "Tampa Bay Rays":         "TBR",
    "Toronto Blue Jays":      "TOR",
    "Baltimore Orioles":      "BAL",
    "Chicago White Sox":      "CHW",
    "Cleveland Guardians":    "CLE",
    "Detroit Tigers":         "DET",
    "Kansas City Royals":     "KCR",
    "Minnesota Twins":        "MIN",
    "Houston Astros":         "HOU",
    "Los Angeles Angels":     "LAA",
    "Oakland Athletics":      "OAK",
    "Athletics":              "OAK",
    "Seattle Mariners":       "SEA",
    "Texas Rangers":          "TEX",
    "Atlanta Braves":         "ATL",
    "Miami Marlins":          "MIA",
    "New York Mets":          "NYM",
    "Philadelphia Phillies":  "PHI",
    "Washington Nationals":   "WSN",
    "Chicago Cubs":           "CHC",
    "Cincinnati Reds":        "CIN",
    "Milwaukee Brewers":      "MIL",
    "Pittsburgh Pirates":     "PIT",
    "St. Louis Cardinals":    "STL",
    "Arizona Diamondbacks":   "ARI",
    "Colorado Rockies":       "COL",
    "Los Angeles Dodgers":    "LAD",
    "San Diego Padres":       "SDP",
    "San Francisco Giants":   "SFG",
}

# League average ERA estimate fallback
FALLBACK_ERA_EST  = 4.20
# League average wRC+ fallback (100 = exactly average)
FALLBACK_WRC_PLUS = 100


def _normalize(name):
    """Lowercase, strip whitespace and common suffixes."""
    if not name:
        return ""
    n = str(name).lower().strip()
    for suf in [" jr.", " sr.", " ii", " iii", " iv", "."]:
        n = n.replace(suf, "")
    return n.strip()


def find_pitcher_stats(pitcher_name, pitchers_df):
    """
    Find a pitcher's ERA_est in the FanGraphs pitching DataFrame.

    Tries full name match first, then first-initial + last-name match.
    Last-name-only fallback removed — too risky when two pitchers share a surname.
    Returns (era_est, row_or_None).
    """
    if not pitcher_name or pitchers_df is None or pitchers_df.empty:
        return FALLBACK_ERA_EST, None

    target = _normalize(pitcher_name)
    parts = target.split()

    # 1. Exact full name
    for _, row in pitchers_df.iterrows():
        if _normalize(row.get("Name", "")) == target:
            return float(row.get("ERA_est", FALLBACK_ERA_EST)), row

    # 2. First initial + last name  (handles middle names / accent variants)
    if len(parts) >= 2:
        target_initial = parts[0][0]
        target_last    = parts[-1]
        candidates = []
        for _, row in pitchers_df.iterrows():
            n = _normalize(row.get("Name", "")).split()
            if len(n) >= 2 and n[0][0] == target_initial and n[-1] == target_last:
                candidates.append(row)
        if len(candidates) == 1:
            row = candidates[0]
            return float(row.get("ERA_est", FALLBACK_ERA_EST)), row

    # 3. Nothing matched — return league average
    return FALLBACK_ERA_EST, None


def find_team_wrc_plus(team_name, team_batting_df):
    """
    Find a team's wRC+ (or OPS+ proxy) from the team batting DataFrame.
    Returns integer, or 100 (league average) if not found.
    The MLB Stats API version stores TeamName (full name) and Team (abbreviation).
    """
    if team_batting_df is None or team_batting_df.empty:
        return FALLBACK_WRC_PLUS

    # 1. Try matching on full team name (MLB API stores 'TeamName')
    if "TeamName" in team_batting_df.columns:
        match = team_batting_df[
            team_batting_df["TeamName"].str.lower() == team_name.lower()
        ]
        if not match.empty and "wRC+" in match.columns:
            try:
                return int(float(match.iloc[0]["wRC+"]))
            except (TypeError, ValueError):
                pass

    # 2. Try abbreviation lookup
    abbrev = TEAM_ABBREV.get(team_name)
    if not abbrev:
        for full_name, ab in TEAM_ABBREV.items():
            if team_name.lower() in full_name.lower():
                abbrev = ab
                break

    if abbrev and "Team" in team_batting_df.columns:
        match = team_batting_df[team_batting_df["Team"] == abbrev]
        if not match.empty and "wRC+" in match.columns:
            try:
                return int(float(match.iloc[0]["wRC+"]))
            except (TypeError, ValueError):
                pass

    # 3. Partial name match as last resort
    if "TeamName" in team_batting_df.columns:
        t_lower = team_name.lower()
        for _, row in team_batting_df.iterrows():
            row_name = str(row.get("TeamName", "")).lower()
            # Match on last word (e.g. "Yankees", "Dodgers")
            if t_lower.split()[-1] in row_name or row_name.split()[-1] in t_lower:
                try:
                    return int(float(row["wRC+"]))
                except (TypeError, ValueError):
                    pass

    return FALLBACK_WRC_PLUS


def get_lineup_hand_pct(lineup_ids, bat_side_dict):
    """
    Compute the fraction of confirmed lineup that bats left vs right.
    Switch hitters count as 0.5 L / 0.5 R.

    Returns {"L": float, "R": float} summing to 1.0.
    Returns None if lineup is unavailable or too few players have known sides.
    """
    if not lineup_ids or not bat_side_dict:
        return None

    l_count = r_count = 0.0
    for pid in lineup_ids:
        side = bat_side_dict.get(str(pid), "R")
        if side == "L":
            l_count += 1.0
        elif side == "S":   # switch hitter — counts as half each
            l_count += 0.5
            r_count += 0.5
        else:
            r_count += 1.0

    total = l_count + r_count
    if total < 6:
        return None

    return {"L": round(l_count / total, 3), "R": round(r_count / total, 3)}


def find_pitcher_split_era(pitcher_name, split_dict, opp_hand_pct, fallback_era):
    """
    Return a platoon-adjusted ERA for a pitcher based on opposing lineup composition.

    opp_hand_pct : {"L": 0.6, "R": 0.4} — fraction of opposing lineup that bats L/R
                   (None if lineup unknown → returns fallback_era unchanged)

    Logic:
      weighted_era = vs_L_era * pct_L + vs_R_era * pct_R
      Then blend 60/40 with the pitcher's overall ERA to avoid overreacting to
      small split samples.
    """
    if not split_dict or not pitcher_name or not opp_hand_pct:
        return fallback_era

    target = _normalize(pitcher_name)

    # Find pitcher in split dict
    splits = None
    for name, data in split_dict.items():
        if _normalize(name) == target:
            splits = data
            break

    if splits is None:
        # Last-name fallback
        target_last = target.split()[-1] if target else ""
        matches = [(n, d) for n, d in split_dict.items()
                   if _normalize(n).split()[-1] == target_last]
        if len(matches) == 1:
            splits = matches[0][1]

    if not splits or "vs_L" not in splits or "vs_R" not in splits:
        return fallback_era

    pct_l = opp_hand_pct.get("L", 0.5)
    pct_r = opp_hand_pct.get("R", 0.5)

    split_era = splits["vs_L"] * pct_l + splits["vs_R"] * pct_r

    # Blend: 60% split-adjusted, 40% overall ERA — split samples are noisier
    return round(0.60 * split_era + 0.40 * fallback_era, 2)


def find_team_k_rate(team_name, team_batting_df):
    """
    Return a team's strikeout rate as batters (SO/PA).
    Used to adjust expected strikeout projections for K props.
    """
    FALLBACK_K_PCT = 0.22   # MLB average strikeout rate

    if team_batting_df is None or team_batting_df.empty or "K_pct" not in team_batting_df.columns:
        return FALLBACK_K_PCT

    # Try full name match
    if "TeamName" in team_batting_df.columns:
        match = team_batting_df[team_batting_df["TeamName"].str.lower() == team_name.lower()]
        if not match.empty:
            try:
                return float(match.iloc[0]["K_pct"])
            except (TypeError, ValueError):
                pass

    # Abbreviation fallback
    abbrev = TEAM_ABBREV.get(team_name)
    if abbrev and "Team" in team_batting_df.columns:
        match = team_batting_df[team_batting_df["Team"] == abbrev]
        if not match.empty:
            try:
                return float(match.iloc[0]["K_pct"])
            except (TypeError, ValueError):
                pass

    return FALLBACK_K_PCT


def get_platoon_flag(pitcher_name, split_dict):
    """
    Returns a short alert string if a pitcher has a significant platoon split,
    otherwise returns an empty string.

    Thresholds (ERA difference between vs_L and vs_R):
      >= 1.5 : LARGE SPLIT  — very likely to be targeted by opposing lineup
      >= 0.75: Split        — worth monitoring when lineup confirms

    Example return: "LARGE SPLIT — weak vs LHB (4.35 / 2.70)"
    """
    if not split_dict or not pitcher_name:
        return ""

    target = _normalize(pitcher_name)
    splits = None

    for name, data in split_dict.items():
        if _normalize(name) == target:
            splits = data
            break

    if splits is None:
        target_last = target.split()[-1] if target else ""
        matches = [(n, d) for n, d in split_dict.items()
                   if _normalize(n).split()[-1] == target_last]
        if len(matches) == 1:
            splits = matches[0][1]

    if not splits or "vs_L" not in splits or "vs_R" not in splits:
        return ""

    era_vs_l = splits["vs_L"]
    era_vs_r = splits["vs_R"]
    diff     = abs(era_vs_l - era_vs_r)

    if diff < 0.75:
        return ""

    worse_vs  = "LHB" if era_vs_l > era_vs_r else "RHB"
    label     = "LARGE SPLIT" if diff >= 1.5 else "Split"

    return f"{label} — weak vs {worse_vs} ({era_vs_l:.2f} / {era_vs_r:.2f})"


def find_pitcher_ha_era(pitcher_name, ha_dict, is_home, fallback_era):
    """
    Return a pitcher's home or road ERA estimate from the home/away split dict.

    is_home    : True  → pitcher is at their home park today
                 False → pitcher is on the road today
    fallback_era: returned unchanged if no split data is found

    Uses same exact/last-name matching as other pitcher lookups.
    """
    if not ha_dict or not pitcher_name:
        return fallback_era

    side   = "home" if is_home else "away"
    target = _normalize(pitcher_name)

    # 1. Exact match
    for name, data in ha_dict.items():
        if _normalize(name) == target:
            return data.get(side, fallback_era)

    # 2. Last-name match
    target_last = target.split()[-1] if target else ""
    matches = [(n, d) for n, d in ha_dict.items()
               if _normalize(n).split()[-1] == target_last]
    if len(matches) == 1:
        return matches[0][1].get(side, fallback_era)

    return fallback_era


def find_pitcher_hand(pitcher_name, hand_dict):
    """
    Look up a pitcher's throwing hand (R/L) from the handedness dict.
    Returns "R" as default if not found (right-handers are more common).
    """
    if not pitcher_name or not hand_dict:
        return "R"

    target = _normalize(pitcher_name)

    # 1. Exact match
    for name, hand in hand_dict.items():
        if _normalize(name) == target:
            return hand

    # 2. Last-name match
    target_last = target.split()[-1] if target else ""
    matches = [(n, h) for n, h in hand_dict.items()
               if _normalize(n).split()[-1] == target_last]
    if len(matches) == 1:
        return matches[0][1]

    return "R"


def find_team_split_wrc(team_name, split_dict, pitcher_hand, fallback_wrc=100):
    """
    Return the team's wRC+ proxy split by opposing pitcher handedness.

    pitcher_hand : "R" or "L" — the starting pitcher's throwing hand
    fallback_wrc : overall team wRC+ to use if split data is unavailable

    If pitcher is TBD or split data is missing, returns fallback_wrc (overall).
    """
    if not split_dict or not team_name or pitcher_hand not in ("R", "L"):
        return fallback_wrc

    split_key = "vs_R" if pitcher_hand == "R" else "vs_L"

    # Find team entry (try exact, then case-insensitive, then last-word)
    team_data = (
        split_dict.get(team_name)
        or next((v for k, v in split_dict.items()
                 if k.lower() == team_name.lower()), None)
        or next((v for k, v in split_dict.items()
                 if team_name.lower().split()[-1] in k.lower()), None)
    )

    if team_data and split_key in team_data:
        return int(team_data[split_key])

    return fallback_wrc


def find_team_bullpen_era(team_name, bullpen_dict, opp_hand_pct=None):
    """
    Look up a team's bullpen ERA estimate, optionally platoon-adjusted.

    bullpen_dict values are now dicts: {"overall": x, "vs_L": x, "vs_R": x}
    opp_hand_pct : {"L": 0.6, "R": 0.4} from the opposing lineup
                   If None, returns overall bullpen ERA.
    """
    if not bullpen_dict:
        return FALLBACK_ERA_EST

    team_lower = team_name.lower()
    data = (
        bullpen_dict.get(team_name)
        or next((v for k, v in bullpen_dict.items() if k.lower() == team_lower), None)
        or next((v for k, v in bullpen_dict.items()
                 if team_lower.split()[-1] in k.lower()), None)
    )

    if data is None:
        return FALLBACK_ERA_EST

    # Handle old format (plain float) for backwards compatibility
    if isinstance(data, (int, float)):
        return float(data)

    overall = data.get("overall", FALLBACK_ERA_EST)

    if opp_hand_pct and "vs_L" in data and "vs_R" in data:
        pct_l = opp_hand_pct.get("L", 0.5)
        pct_r = opp_hand_pct.get("R", 0.5)
        split_era = data["vs_L"] * pct_l + data["vs_R"] * pct_r
        # Blend 60% split / 40% overall
        return round(0.60 * split_era + 0.40 * overall, 2)

    return overall


def _batter_pa_weight(pa):
    """
    Mirrors _prior_weight() in run.py, scaled for individual batter PA.
    1 IP-equivalent = 3 PA  →  fully trusted at ~288 PA (roughly half a starter season).
    Same breakpoints as the pitcher curve: 30% at 36 PA, 60% at 72, 80% at 144, 100% at 288.
    """
    ip = pa / 3.0
    if ip <= 0:  return 0.0
    if ip >= 96: return 1.0
    if ip <= 12: return 0.30 * ip / 12
    if ip <= 24: return 0.30 + 0.30 * (ip - 12) / 12
    if ip <= 48: return 0.60 + 0.20 * (ip - 24) / 24
    return        0.80 + 0.20 * (ip - 48) / 48


def adjust_wrc_for_lineup(lineup_ids, team_wrc, batter_wrc_dict):
    """
    Adjust a team's wRC+ based on who is actually in today's confirmed lineup.
    Falls back to team_wrc unchanged if lineup is missing or too few players have data.

    Each batter's individual wRC+ is regressed toward league average (100) by PA to
    reduce April small-sample noise.  The resulting lineup average is then blended
    60% team season / 40% confirmed lineup, capped at ±15 points.

    batter_wrc_dict values may be {"wrc": x, "pa": y} (current format) or a plain
    number (legacy cache) — both are handled.
    """
    MIN_PLAYERS = 6
    LG_AVG = 100

    if not lineup_ids or not batter_wrc_dict:
        return team_wrc

    lineup_wrc_vals = []
    for pid in lineup_ids:
        entry = batter_wrc_dict.get(pid)
        if entry is None:
            continue
        if isinstance(entry, dict):
            raw_wrc = entry.get("wrc", LG_AVG)
            pa      = entry.get("pa", 0)
        else:
            raw_wrc = entry   # legacy plain-number format
            pa      = 0

        # Regress toward league avg (100) based on PA sample size
        w = _batter_pa_weight(pa)
        regressed = w * raw_wrc + (1 - w) * LG_AVG
        lineup_wrc_vals.append(regressed)

    if len(lineup_wrc_vals) < MIN_PLAYERS:
        return team_wrc

    lineup_avg_wrc = sum(lineup_wrc_vals) / len(lineup_wrc_vals)

    # Blend: 60% team season stats, 40% today's confirmed lineup
    adjusted = 0.60 * team_wrc + 0.40 * lineup_avg_wrc

    # Cap the change at ±15 points
    adjusted = max(team_wrc - 15, min(adjusted, team_wrc + 15))

    return round(adjusted)


def match_odds_game(away_team, home_team, odds_dict, game_time=None):
    """
    Find the matching game in the Odds API response dict.

    The Odds API may use slightly different team names than the MLB API,
    so we do a partial last-word match.

    For doubleheaders the dict contains "Away @ Home" and "Away @ Home (2)".
    When game_time is provided we pick the entry whose commence_str is closest
    to the MLB game start time.  Without a game_time we fall back to the first
    (earliest) matching entry.
    """
    from datetime import datetime, timezone

    def _parse_utc(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    game_dt = _parse_utc(game_time)

    # Collect all candidate (key, value) pairs that match this matchup
    away_last = away_team.split()[-1].lower()
    home_last = home_team.split()[-1].lower()
    candidates = []
    for k, v in odds_dict.items():
        k_lower = k.lower()
        if away_last in k_lower and home_last in k_lower:
            candidates.append((k, v))

    if not candidates:
        print(f"    Odds match: no match found for '{away_team} @ {home_team}'")
        return None

    if len(candidates) == 1:
        k, v = candidates[0]
        if k != f"{away_team} @ {home_team}":
            print(f"    Odds match: '{away_team} @ {home_team}' → '{k}' (partial)  total={v['total']['line']}")
        return v

    # Multiple candidates (doubleheader) — pick the one with the nearest start time
    if game_dt is not None:
        best_k, best_v, best_diff = None, None, float("inf")
        for k, v in candidates:
            cdt = _parse_utc(v.get("commence_str"))
            if cdt is not None:
                diff = abs((cdt - game_dt).total_seconds())
                if diff < best_diff:
                    best_diff = diff
                    best_k, best_v = k, v
        if best_k is not None:
            print(f"    Odds match: '{away_team} @ {home_team}' → '{best_k}' (doubleheader, time-matched)")
            return best_v

    # Fallback: return earliest entry
    k, v = candidates[0]
    print(f"    Odds match: '{away_team} @ {home_team}' → '{k}' (first of {len(candidates)} entries)")
    return v
