"""
Stats Fetcher
=============
Pulls pitcher and team stats from the FREE MLB Stats API.
No scraping, no API key, no rate limit concerns.

Pitcher ERA estimator: FIP (Fielding Independent Pitching)
  FIP = (13*HR + 3*(BB+HBP) - 2*K) / IP + FIP_constant
  FIP strips out defense and luck — only strikeouts, walks, and homers.
  Very similar in predictive power to xFIP/SIERA for a first model.

Team offense: true wRC+ computed from wOBA using standard FanGraphs linear weights.
  wOBA = (wBB*BB + wHBP*HBP + w1B*1B + w2B*2B + w3B*3B + wHR*HR) / PA_denominator
  wRC+ = ((wOBA - lg_wOBA) / wOBA_scale / (lg_R/lg_PA) + 1) / park_factor_adj * 100
  Park adjustment removes home park effect so the model can apply it cleanly.
"""

import csv
import io
import os
import json
import requests
from datetime import date

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(CACHE_DIR, exist_ok=True)

MLB_API = "https://statsapi.mlb.com/api/v1"

# FIP constant: the number added to make league-average FIP = league-average ERA
# Typically ~3.10-3.20 in modern MLB. We estimate it dynamically below.
FALLBACK_FIP_CONSTANT = 3.20

FALLBACK_ERA_EST  = 4.25   # IP-weighted ERA of spot starters (1-4 GS), 2024-2025 data
FALLBACK_WRC_PLUS = 100

# FanGraphs linear weights (stable across recent seasons, 2023-2025 averages)
W_BB       = 0.690
W_HBP      = 0.722
W_1B       = 0.881
W_2B       = 1.256
W_3B       = 1.594
W_HR       = 2.058
WOBA_SCALE = 1.157    # converts wOBA to run value per PA
LG_R_PER_PA = 0.120  # fallback: ~4.43 runs / ~37 PA per game


def _compute_woba(h, doubles, triples, hr, bb, ibb, hbp, sf, ab):
    """Compute wOBA from raw counting stats."""
    singles     = max(0, h - doubles - triples - hr)
    numerator   = (W_BB * max(0, bb - ibb) + W_HBP * hbp +
                   W_1B * singles + W_2B * doubles +
                   W_3B * triples + W_HR * hr)
    denominator = ab + max(0, bb - ibb) + sf + hbp
    return round(numerator / denominator, 4) if denominator > 0 else 0.320


def _woba_to_wrc_plus(woba, lg_woba, lg_r_per_pa, park_factor):
    """
    Convert wOBA to park-adjusted wRC+.
    park_factor: team's home park factor (e.g. 1.13 for Coors).
    park_factor_adj = PF * 0.5 + 0.5 (half games at home).
    """
    if lg_r_per_pa <= 0:
        lg_r_per_pa = LG_R_PER_PA
    park_factor_adj = park_factor * 0.5 + 0.5
    raw = ((woba - lg_woba) / WOBA_SCALE / lg_r_per_pa + 1.0) / park_factor_adj * 100
    return max(50, min(int(round(raw)), 160))


# --------------------------------------------------------------------------- #
# Pitcher stats
# --------------------------------------------------------------------------- #

def get_pitcher_stats(season):
    """
    Pull all pitcher stats for the season from the MLB Stats API.
    Computes FIP as our ERA estimator.
    Returns a DataFrame with: Name, Team, IP, ERA, FIP (as ERA_est), K%, BB%.
    """
    import pandas as pd

    cache_file = os.path.join(CACHE_DIR, f"pitchers_{season}_{date.today()}.csv")

    if os.path.exists(cache_file):
        print("  (Using cached pitcher data from today)")
        return pd.read_csv(cache_file)

    print("  Downloading pitcher stats from MLB Stats API ...")

    url = f"{MLB_API}/stats"
    params = {
        "stats":      "season",
        "group":      "pitching",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "limit":      2000,       # plenty for a full roster
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: MLB API pitcher fetch failed — {e}")
        print("  Using league-average fallback for all pitchers.")
        return pd.DataFrame()

    splits = data.get("stats", [{}])[0].get("splits", [])

    rows = []
    all_fip_components = []  # for computing FIP constant

    for split in splits:
        s    = split.get("stat", {})
        name = split.get("player", {}).get("fullName", "Unknown")
        team = split.get("team", {}).get("name", "")

        ip_str = str(s.get("inningsPitched", "0.0"))
        ip     = _parse_ip(ip_str)

        if ip <= 0:            # skip pitchers with no recorded work
            continue

        k   = int(s.get("strikeOuts", 0))
        bb  = int(s.get("baseOnBalls", 0))
        hr  = int(s.get("homeRuns", 0))
        hbp = int(s.get("hitBatsmen", 0))
        h   = int(s.get("hits", 0))
        bf  = int(s.get("battersFaced", 0))
        era_str = str(s.get("era", "4.50"))
        gs  = int(s.get("gamesStarted", 0))

        try:
            era = float(era_str)
        except ValueError:
            era = 4.50

        # FIP numerator (before dividing by IP and adding constant)
        fip_num = (13 * hr) + (3 * (bb + hbp)) - (2 * k)
        all_fip_components.append((fip_num, ip))

        k_pct  = round(k  / bf, 4) if bf > 0 else 0.20
        bb_pct = round(bb / bf, 4) if bf > 0 else 0.08

        # H/9: regress toward league average (8.8) at same rate as FIP
        _LG_H9  = 8.8
        _h9_raw = (h / ip * 9.0) if ip > 0 else _LG_H9
        _h9_w   = min(ip / 60.0, 1.0)
        h9      = round(_h9_raw * _h9_w + _LG_H9 * (1.0 - _h9_w), 2)

        rows.append({
            "Name":   name,
            "Team":   team,
            "GS":     gs,
            "IP":     round(ip, 1),
            "ERA":    era,
            "K%":     k_pct,
            "BB%":    bb_pct,
            "H9":     h9,
            "_fip_num": fip_num,
            "_ip":      ip
        })

    if not rows:
        print("  WARNING: No pitcher data returned from MLB API.")
        return pd.DataFrame()

    # Compute FIP constant so league-average FIP ≈ league-average ERA
    total_fip_num = sum(r["_fip_num"] for r in rows)
    total_ip      = sum(r["_ip"]      for r in rows)
    league_era    = sum(r["ERA"] * r["_ip"] for r in rows) / max(total_ip, 1)
    fip_constant  = league_era - (total_fip_num / max(total_ip, 1))

    # Compute each pitcher's FIP with regression to the mean.
    #
    # Problem: a pitcher with 3 innings and 0 HR/BB looks elite (FIP ~1.50),
    # but 3 innings is noise.  We weight FIP vs. league average based on sample size.
    # At 60+ IP we trust FIP fully. At 10 IP we're ~17% FIP, 83% league average.
    # Formula: ERA_est = FIP * weight + LEAGUE_AVG * (1 - weight)
    #          weight  = min(ip / 60, 1.0)
    #
    REGRESSION_IP = 60.0   # IP needed to fully trust FIP

    for r in rows:
        ip = r["_ip"]
        if ip > 0:
            raw_fip = (r["_fip_num"] / ip) + fip_constant
            raw_fip = max(1.50, min(raw_fip, 9.00))

            weight = min(ip / REGRESSION_IP, 1.0)
            r["raw_fip"] = round(raw_fip, 2)
            r["ERA_est"] = round(
                raw_fip * weight + FALLBACK_ERA_EST * (1.0 - weight), 2
            )
        else:
            r["raw_fip"] = FALLBACK_ERA_EST
            r["ERA_est"] = FALLBACK_ERA_EST

        del r["_fip_num"]
        del r["_ip"]

    import pandas as pd
    df = pd.DataFrame(rows)
    df.to_csv(cache_file, index=False)
    print(f"  Loaded {len(df)} pitchers  |  FIP constant: {fip_constant:.2f}")
    return df


# --------------------------------------------------------------------------- #
# Bullpen stats
# --------------------------------------------------------------------------- #

def get_bullpen_stats(season, pitchers_df=None):
    """
    Compute team bullpen ERA estimates from pitcher stats, including platoon splits.

    A reliever is any pitcher with GS == 0 (never started a game).
    Returns a dict: {
        team_full_name: {
            "overall": era_est,
            "vs_L":    era_est,   # bullpen ERA vs left-handed batters
            "vs_R":    era_est,   # bullpen ERA vs right-handed batters
        }
    }
    """
    import pandas as pd

    cache_file = os.path.join(CACHE_DIR, f"bullpen2_{season}_{date.today()}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cached = json.load(f)
        if cached:
            return cached
        os.remove(cache_file)

    if pitchers_df is None or pitchers_df.empty:
        pitchers_df = get_pitcher_stats(season)

    if pitchers_df is None or pitchers_df.empty:
        return {}

    relievers = pitchers_df[
        (pitchers_df["GS"] == 0) & (pitchers_df["IP"] >= 3.0)
    ].copy()

    if relievers.empty:
        return {}

    # Load pitcher splits to get bullpen vs L / vs R
    pitcher_splits = get_pitcher_split_stats(season)

    team_bullpen = {}
    for team_name, group in relievers.groupby("Team"):
        total_ip = group["IP"].sum()
        if total_ip < 10:
            continue

        # Use raw ERA (not individually-regressed ERA_est) then apply ONE
        # regression at the team level using total bullpen IP.
        # Early in the season each reliever has ~5-10 IP so ERA_est collapses
        # to league average (4.20) for everyone. Total bullpen IP (~40-70) is
        # large enough to show real team differences.
        raw_era = (group["ERA"] * group["IP"]).sum() / total_ip
        raw_era = max(1.50, min(raw_era, 9.00))
        team_weight = min(total_ip / 60.0, 1.0)
        overall = raw_era * team_weight + FALLBACK_ERA_EST * (1.0 - team_weight)

        # Compute split bullpen ERA weighted by each reliever's IP
        vs_l_num = vs_l_den = vs_r_num = vs_r_den = 0.0
        for _, row in group.iterrows():
            name = row["Name"]
            ip   = row["IP"]
            splits = pitcher_splits.get(name, {})
            if "vs_L" in splits:
                vs_l_num += splits["vs_L"] * ip
                vs_l_den += ip
            if "vs_R" in splits:
                vs_r_num += splits["vs_R"] * ip
                vs_r_den += ip

        team_bullpen[team_name] = {
            "overall": round(overall, 2),
            "vs_L":    round(vs_l_num / vs_l_den, 2) if vs_l_den > 0 else round(overall, 2),
            "vs_R":    round(vs_r_num / vs_r_den, 2) if vs_r_den > 0 else round(overall, 2),
        }

    eras = sorted([(t, v["overall"]) for t, v in team_bullpen.items()], key=lambda x: x[1])
    if eras:
        print(f"  Bullpen ERA range: {eras[0][1]} ({eras[0][0].split()[-1]}) "
              f"→ {eras[-1][1]} ({eras[-1][0].split()[-1]})")

    with open(cache_file, "w") as f:
        json.dump(team_bullpen, f, indent=2)

    print(f"  Bullpen ERA computed for {len(team_bullpen)} teams (with L/R splits)")
    return team_bullpen


# --------------------------------------------------------------------------- #
# Individual batter stats (for lineup adjustments)
# --------------------------------------------------------------------------- #

def get_batter_stats(season):
    """
    Pull individual batter stats for the season.
    Used to adjust team wRC+ when a confirmed lineup is available.
    Returns a dict: {player_id: wrc_plus} computed from counting stats.
    """
    cache_file = os.path.join(CACHE_DIR, f"batters_wrc_{season}_{date.today()}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print("  Downloading batter stats from MLB Stats API ...")

    url = f"{MLB_API}/stats"
    params = {
        "stats":      "season",
        "group":      "hitting",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "limit":      2000,
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Batter stats fetch failed — {e}")
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])
    rows = []

    for split in splits:
        s      = split.get("stat", {})
        player = split.get("player", {})
        pid    = player.get("id")
        pa     = int(s.get("plateAppearances", 0))

        if not pid or pa < 20:
            continue

        try:
            h   = int(s.get("hits",               0))
            d   = int(s.get("doubles",             0))
            t   = int(s.get("triples",             0))
            hr  = int(s.get("homeRuns",            0))
            bb  = int(s.get("baseOnBalls",         0))
            ibb = int(s.get("intentionalWalks",    0))
            hbp = int(s.get("hitByPitch",          0))
            sf  = int(s.get("sacFlies",            0))
            ab  = int(s.get("atBats",              0))
            r   = int(s.get("runs",                0))
        except (TypeError, ValueError):
            continue

        woba = _compute_woba(h, d, t, hr, bb, ibb, hbp, sf, ab)
        rows.append({"pid": str(pid), "woba": woba, "pa": pa, "r": r})

    if not rows:
        return {}

    # Derive league averages from the same player pool
    total_pa    = sum(row["pa"] for row in rows)
    total_r     = sum(row["r"]  for row in rows)
    lg_woba     = sum(row["woba"] * row["pa"] for row in rows) / total_pa if total_pa > 0 else 0.320
    lg_r_per_pa = total_r / total_pa if total_pa > 0 and total_r > 0 else LG_R_PER_PA

    # Neutral park factor — park adj is already baked into the team wRC+ we blend with
    # Store {"wrc": ..., "pa": ...} so callers can apply PA-based regression.
    batter_wrc = {}
    for row in rows:
        wrc = _woba_to_wrc_plus(row["woba"], lg_woba, lg_r_per_pa, park_factor=1.0)
        batter_wrc[row["pid"]] = {"wrc": wrc, "pa": row["pa"]}

    with open(cache_file, "w") as f:
        json.dump(batter_wrc, f)

    print(f"  {len(batter_wrc)} batters loaded (wRC+)  |  lg wOBA: {lg_woba:.3f}")
    return batter_wrc


# --------------------------------------------------------------------------- #
# Pitcher handedness
# --------------------------------------------------------------------------- #

def get_player_handedness(season):
    """
    Fetch pitching hand (R/L) for all players via the MLB people endpoint.
    Returns dict: {full_name: "R"|"L"}
    Used to look up whether each probable starter throws right or left.
    """
    cache_file = os.path.join(CACHE_DIR, f"handedness_{season}_{date.today()}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print("  Downloading player handedness from MLB Stats API ...")

    url = f"{MLB_API}/sports/1/players"
    params = {"season": season}

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Handedness fetch failed — {e}")
        return {}

    hand_dict    = {}
    bat_side_dict = {}
    bat_cache = os.path.join(CACHE_DIR, f"bat_side_{season}_{date.today()}.json")

    for player in data.get("people", []):
        name = player.get("fullName")
        pid  = str(player.get("id", ""))
        hand = player.get("pitchHand", {}).get("code", "R")
        bat  = player.get("batSide",   {}).get("code", "R")
        if name:
            hand_dict[name] = hand
        if pid:
            bat_side_dict[pid] = bat

    with open(cache_file, "w") as f:
        json.dump(hand_dict, f)
    with open(bat_cache, "w") as f:
        json.dump(bat_side_dict, f)

    print(f"  {len(hand_dict)} player handedness records loaded")
    return hand_dict


def get_player_bat_side(season):
    """
    Returns {player_id_str: bat_side} where bat_side is "L", "R", or "S" (switch).
    Used to compute lineup handedness composition for platoon split adjustments.
    Shares the same API call as get_player_handedness (no extra requests).
    """
    bat_cache = os.path.join(CACHE_DIR, f"bat_side_{season}_{date.today()}.json")
    if os.path.exists(bat_cache):
        with open(bat_cache) as f:
            return json.load(f)

    # Cache not present — trigger the handedness fetch which saves bat side too
    get_player_handedness(season)

    if os.path.exists(bat_cache):
        with open(bat_cache) as f:
            return json.load(f)

    return {}


# --------------------------------------------------------------------------- #
# Team batting splits (vs LHP / vs RHP)
# --------------------------------------------------------------------------- #

def get_team_split_stats(season):
    """
    Fetch each team's wRC+ split by opposing pitcher handedness.
    Returns dict: {team_name: {"vs_R": wrc_plus, "vs_L": wrc_plus}}

    Uses true wRC+ (wOBA-based) normalized to split-specific league average.
    Park adjustment applied at half-weight (splits mix home and away games).
    """
    cache_file = os.path.join(CACHE_DIR, f"team_splits_wrc_{season}_{date.today()}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print("  Downloading team split stats from MLB Stats API ...")

    try:
        from model import PARK_FACTORS
    except ImportError:
        PARK_FACTORS = {}

    raw = {}  # team_name -> {label -> {woba, pa, r}}

    for sit_code, label in [("vr", "vs_R"), ("vl", "vs_L")]:
        url = f"{MLB_API}/teams/stats"
        params = {
            "stats":    "statSplits",
            "group":    "hitting",
            "season":   season,
            "sportId":  1,
            "gameType": "R",
            "sitCodes": sit_code,
        }

        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WARNING: Team split stats ({sit_code}) failed — {e}")
            continue

        for group in data.get("stats", []):
            for split in group.get("splits", []):
                team_name = split.get("team", {}).get("name", "")
                s = split.get("stat", {})
                if not team_name:
                    continue
                try:
                    h   = int(s.get("hits",            0))
                    d   = int(s.get("doubles",          0))
                    t   = int(s.get("triples",          0))
                    hr  = int(s.get("homeRuns",         0))
                    bb  = int(s.get("baseOnBalls",      0))
                    ibb = int(s.get("intentionalWalks", 0))
                    hbp = int(s.get("hitByPitch",       0))
                    sf  = int(s.get("sacFlies",         0))
                    ab  = int(s.get("atBats",           0))
                    pa  = int(s.get("plateAppearances", 0))
                    r   = int(s.get("runs",             0))
                except (TypeError, ValueError):
                    continue

                if pa < 1:
                    continue

                woba = _compute_woba(h, d, t, hr, bb, ibb, hbp, sf, ab)
                raw.setdefault(team_name, {})[label] = {"woba": woba, "pa": pa, "r": r}

    if not raw:
        return {}

    result = {}
    for sit_label in ["vs_R", "vs_L"]:
        entries = [(tn, v[sit_label]) for tn, v in raw.items() if sit_label in v]
        if not entries:
            continue

        total_pa    = sum(e["pa"] for _, e in entries)
        total_r     = sum(e["r"]  for _, e in entries)
        lg_r_per_pa = total_r / total_pa if total_pa > 0 and total_r > 0 else LG_R_PER_PA
        lg_woba     = sum(e["woba"] * e["pa"] for _, e in entries) / total_pa if total_pa > 0 else 0.320

        for team_name, entry in entries:
            pf = PARK_FACTORS.get(team_name, 1.0)
            # Half-weight park adj: splits mix home/away so park effect is diluted
            pf_adj = pf * 0.25 + 0.75
            wrc = _woba_to_wrc_plus(entry["woba"], lg_woba, lg_r_per_pa, pf)
            wrc = max(50, min(wrc, 160))
            result.setdefault(team_name, {})[sit_label] = wrc

    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)

    print(f"  Team split stats loaded for {len(result)} teams")
    return result


# --------------------------------------------------------------------------- #
# Pitcher splits (ERA vs LHB / vs RHB)
# --------------------------------------------------------------------------- #

def get_pitcher_home_away_stats(season):
    """
    Fetch each pitcher's ERA split by home games vs road games.
    Returns {pitcher_name: {"home": era_est, "away": era_est}}

    Uses statSplits with sitCodes h (home) and r (road) — same pattern
    as vs_L / vs_R platoon splits which are confirmed working.
    Uses same FIP regression as platoon splits (40 IP threshold).
    """
    cache_file = os.path.join(CACHE_DIR, f"pitcher_ha_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cached = json.load(f)
        # Discard stale empty caches (from previous failed attempts)
        if cached:
            return cached
        os.remove(cache_file)

    print("  Downloading pitcher home/away split stats ...")
    SPLIT_REGRESSION_IP = 40.0

    result = {}

    for sit_code, label in [("h", "home"), ("r", "away")]:
        url    = f"{MLB_API}/stats"
        params = {
            "stats":      "statSplits",
            "group":      "pitching",
            "season":     season,
            "playerPool": "all",
            "sportId":    1,
            "gameType":   "R",
            "sitCodes":   sit_code,
            "limit":      2000,
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WARNING: Pitcher H/A split ({sit_code}) failed — {e}")
            continue

        for group in data.get("stats", []):
            for split in group.get("splits", []):
                name = split.get("player", {}).get("fullName", "")
                s    = split.get("stat", {})
                if not name:
                    continue

                ip = _parse_ip(str(s.get("inningsPitched", "0.0")))
                if ip < 3:
                    continue

                try:
                    era = float(s.get("era") or FALLBACK_ERA_EST)
                    era = max(1.50, min(era, 9.00))
                except (TypeError, ValueError):
                    era = FALLBACK_ERA_EST

                weight  = min(ip / SPLIT_REGRESSION_IP, 1.0)
                era_est = round(era * weight + FALLBACK_ERA_EST * (1.0 - weight), 2)
                result.setdefault(name, {})[label] = era_est

    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Pitcher H/A splits loaded for {len(result)} pitchers")
    return result


def get_pitcher_split_stats(season):
    """
    Fetch each pitcher's ERA split by opposing batter handedness.
    Returns dict: {pitcher_name: {"vs_L": era_est, "vs_R": era_est}}

    Uses heavier regression than full ERA because split samples are smaller
    (~half the innings of full season stats).
    """
    cache_file = os.path.join(CACHE_DIR, f"pitcher_splits_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print("  Downloading pitcher split stats from MLB Stats API ...")
    SPLIT_REGRESSION_IP = 40.0   # IP needed to fully trust split ERA

    result = {}

    for sit_code, label in [("vl", "vs_L"), ("vr", "vs_R")]:
        url    = f"{MLB_API}/stats"
        params = {
            "stats":      "statSplits",
            "group":      "pitching",
            "season":     season,
            "playerPool": "all",
            "sportId":    1,
            "gameType":   "R",
            "sitCodes":   sit_code,
            "limit":      2000,
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WARNING: Pitcher split ({sit_code}) failed — {e}")
            continue

        for group in data.get("stats", []):
            for split in group.get("splits", []):
                name = split.get("player", {}).get("fullName", "")
                s    = split.get("stat", {})
                if not name:
                    continue

                ip = _parse_ip(str(s.get("inningsPitched", "0.0")))
                if ip < 3:
                    continue

                try:
                    era = float(s.get("era") or FALLBACK_ERA_EST)
                    era = max(1.50, min(era, 9.00))
                except (TypeError, ValueError):
                    era = FALLBACK_ERA_EST

                weight  = min(ip / SPLIT_REGRESSION_IP, 1.0)
                era_est = round(era * weight + FALLBACK_ERA_EST * (1.0 - weight), 2)

                result.setdefault(name, {})[label] = era_est

    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Pitcher split stats loaded for {len(result)} pitchers")
    return result


# --------------------------------------------------------------------------- #
# Rest days
# --------------------------------------------------------------------------- #

def get_rest_days(target_date=None):
    """
    Returns {team_name: days_since_last_game} by scanning the past 7 days.
    Used to apply small fatigue/rust adjustments to expected runs.
    """
    from datetime import timedelta

    if target_date is None:
        target_date = date.today()
    elif isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    cache_file = os.path.join(CACHE_DIR, f"rest_{target_date}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    start_str = (target_date - timedelta(days=7)).strftime("%Y-%m-%d")
    end_str   = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")

    url    = f"{MLB_API}/schedule"
    params = {"sportId": 1, "startDate": start_str, "endDate": end_str, "gameType": "R"}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Rest days fetch failed — {e}")
        return {}

    last_game = {}
    for date_entry in data.get("dates", []):
        try:
            game_date = date.fromisoformat(date_entry.get("date", ""))
        except ValueError:
            continue
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState", "") != "Final":
                continue
            teams = game.get("teams", {})
            for side in ("away", "home"):
                team = teams.get(side, {}).get("team", {}).get("name", "")
                if team and (team not in last_game or game_date > last_game[team]):
                    last_game[team] = game_date

    rest_dict = {team: (target_date - last).days for team, last in last_game.items()}

    with open(cache_file, "w") as f:
        json.dump(rest_dict, f)
    return rest_dict


# --------------------------------------------------------------------------- #
# Bullpen availability (most recent game, with rest-day decay)
# --------------------------------------------------------------------------- #

# Rest-day decay: calibrated against 4,894 team-games (2025 full season).
# 1 day ago: full adjustment; 2 days ago: 50% (observed 0.32 ERA drop); 3+: 0%
_REST_DECAY = {1: 1.0, 2: 0.5}   # days_since_game → multiplier; missing = 0.0


def get_bullpen_usage(target_date=None):
    """
    For each team, find their most recent game (up to 3 days back) and score
    how much bullpen fatigue carries into today's game.

    Rest-day decay is applied so a team that played hard Sunday but had Monday
    off gets half the penalty on Tuesday, not the full penalty.

    ERA adjustment (calibrated against 4,894 team-games, 2025 full season):
      "normal"  (2+ questionable arms, 21-35p): +0.20 ERA — showed +0.36 observed
      "taxed"   (1+ unavailable arm, 36+p):     +0.15 per arm, cap +0.30
      Decay at 2 days rest: 50% of the above (observed 0.32 ERA drop with extra day off)
      Decay at 3+ days rest: 0%

    Note: effect is real but small and noisy (taxed 95% CI: -0.20 to +0.53).
    Signal is primarily a warning flag rather than a strong model input.

    Returns: {
        team_name: {
            "unavailable":      [(name, pitches), ...],
            "questionable":     [(name, pitches), ...],
            "era_adjustment":   float,
            "status":           "fresh" | "normal" | "taxed",
            "total_bp_pitches": int,
            "relievers_used":   int,
            "days_since_game":  int,   # 1 = played yesterday, 2 = 1 off day, etc.
        }
    }
    """
    from datetime import timedelta

    if target_date is None:
        target_date = date.today()
    elif isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    cache_file = os.path.join(CACHE_DIR, f"bullpen_usage_{target_date}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    # Step 1: fetch completed games for each of the past 3 days.
    # Build {team_name: (game_pk, days_since)} keeping only the most recent game.
    most_recent: dict[str, tuple[int, int]] = {}   # team → (pk, days_ago)

    for days_ago in (1, 2, 3):
        check_date = target_date - timedelta(days=days_ago)
        try:
            resp = requests.get(f"{MLB_API}/schedule", params={
                "sportId": 1,
                "date": check_date.strftime("%Y-%m-%d"),
                "gameType": "R",
            }, timeout=15)
            resp.raise_for_status()
            sched = resp.json()
        except Exception as e:
            if days_ago == 1:
                print(f"  WARNING: Bullpen usage fetch failed — {e}")
            continue

        for date_entry in sched.get("dates", []):
            for game in date_entry.get("games", []):
                if game.get("status", {}).get("abstractGameState", "") != "Final":
                    continue
                pk = game["gamePk"]
                for side in ("away", "home"):
                    name = game["teams"][side]["team"]["name"]
                    # Only record if this is more recent than any already found
                    if name not in most_recent:
                        most_recent[name] = (pk, days_ago)

    if not most_recent:
        return {}

    # Step 2: fetch each unique boxscore once, extract reliever pitch counts.
    # team_name → {"usage": {name: pitches}, "days_since": int}
    pk_to_box: dict[int, dict] = {}
    for pk, _ in most_recent.values():
        if pk not in pk_to_box:
            try:
                r = requests.get(f"{MLB_API}/game/{pk}/boxscore", timeout=15)
                r.raise_for_status()
                pk_to_box[pk] = r.json()
            except Exception:
                pk_to_box[pk] = {}

    team_data_map: dict[str, dict] = {}
    for team_name, (pk, days_ago) in most_recent.items():
        box = pk_to_box.get(pk, {})
        usage: dict[str, int] = {}

        for side in ("away", "home"):
            td = box.get("teams", {}).get(side, {})
            if td.get("team", {}).get("name", "") != team_name:
                continue
            pitchers = td.get("pitchers", [])
            players  = td.get("players", {})
            for i, pid in enumerate(pitchers):
                if i == 0:
                    continue
                pobj    = players.get(f"ID{pid}", {})
                pname   = pobj.get("person", {}).get("fullName", f"ID{pid}")
                pstats  = pobj.get("stats", {}).get("pitching", {})
                pitches = pstats.get("numberOfPitches") or pstats.get("pitchesThrown") or 0
                try:
                    pitches = int(pitches)
                except (TypeError, ValueError):
                    pitches = 0
                if pitches > 0:
                    usage[pname] = pitches
            break

        team_data_map[team_name] = {"usage": usage, "days_since": days_ago}

    # Step 3: score each team with rest-day decay applied.
    result = {}
    for team_name, td in team_data_map.items():
        usage     = td["usage"]
        days_ago  = td["days_since"]
        decay     = _REST_DECAY.get(days_ago, 0.0)

        unavailable = [(n, p) for n, p in usage.items() if p >= 36]
        questionable = [(n, p) for n, p in usage.items() if 21 <= p < 36]
        total_bp_pitches = sum(usage.values())
        relievers_used   = len(usage)

        # ERA adjustments calibrated against 4,894 team-games (2025 full season).
        # "normal" (2+ questionable arms) showed larger observed lift (+0.36) than
        # "taxed" (1+ unavailable), so both get a modest adjustment.
        if len(unavailable) >= 1:
            base_adj = min(len(unavailable) * 0.15, 0.30)
            status   = "taxed"
        elif len(questionable) >= 2 and decay > 0:
            base_adj = 0.20
            status   = "normal"
        else:
            base_adj = 0.0
            status   = "fresh"

        era_adj = round(base_adj * decay, 2)

        # Fresh bonus: 2025 data shows well-rested bullpens ERA ~0.13 below season avg.
        # Apply conservatively: -0.10 for genuine extra day off, -0.05 for barely-used.
        if status == "fresh":
            if days_ago >= 2:
                fresh_bonus = -0.10
            elif total_bp_pitches <= 15:
                fresh_bonus = -0.05
            else:
                fresh_bonus = 0.0
        else:
            fresh_bonus = 0.0

        era_adj = round(era_adj + fresh_bonus, 2)

        result[team_name] = {
            "unavailable":      sorted(unavailable, key=lambda x: -x[1]),
            "questionable":     sorted(questionable, key=lambda x: -x[1]),
            "era_adjustment":   era_adj,
            "status":           status,
            "total_bp_pitches": total_bp_pitches,
            "relievers_used":   relievers_used,
            "days_since_game":  days_ago,
            "decay":            decay,
        }

    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)

    return result


# --------------------------------------------------------------------------- #
# Recent form (last 30 days)
# --------------------------------------------------------------------------- #

def get_pitcher_recent_form(season, days=30):
    """
    Pull pitcher stats for the last {days} days and compute a recent ERA estimate.
    Returns {pitcher_name: recent_era_est}

    Minimum 5 IP in the window required (otherwise too noisy to use).
    Regression is heavier than the season model because samples are smaller.
    """
    from datetime import timedelta

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    cache_file = os.path.join(CACHE_DIR,
                              f"pitcher_recent_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print(f"  Downloading pitcher recent form ({days}-day) ...")

    url = f"{MLB_API}/stats"
    params = {
        "stats":      "byDateRange",
        "group":      "pitching",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "limit":      2000,
        "startDate":  start_date.strftime("%m/%d/%Y"),
        "endDate":    end_date.strftime("%m/%d/%Y"),
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Pitcher recent form fetch failed — {e}")
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])

    rows = []
    for split in splits:
        s    = split.get("stat", {})
        name = split.get("player", {}).get("fullName", "")
        if not name:
            continue

        ip = _parse_ip(str(s.get("inningsPitched", "0.0")))
        if ip < 5.0:     # need at least 5 IP in the window
            continue

        k   = int(s.get("strikeOuts",  0))
        bb  = int(s.get("baseOnBalls", 0))
        hr  = int(s.get("homeRuns",    0))
        hbp = int(s.get("hitBatsmen",  0))

        try:
            era = float(str(s.get("era", FALLBACK_ERA_EST)))
        except ValueError:
            era = FALLBACK_ERA_EST

        fip_num = (13 * hr) + (3 * (bb + hbp)) - (2 * k)
        rows.append({"name": name, "ip": ip, "era": era, "_fip_num": fip_num})

    if not rows:
        with open(cache_file, "w") as f:
            json.dump({}, f)
        return {}

    # Dynamic FIP constant from this window
    total_fip_num = sum(r["_fip_num"] for r in rows)
    total_ip      = sum(r["ip"]       for r in rows)
    league_era    = sum(r["era"] * r["ip"] for r in rows) / max(total_ip, 1)
    fip_constant  = league_era - (total_fip_num / max(total_ip, 1))

    # Heavier regression than full-season model (smaller samples)
    RECENT_REGRESSION_IP = 25.0

    result = {}
    for r in rows:
        ip = r["ip"]
        if ip > 0:
            raw_fip = (r["_fip_num"] / ip) + fip_constant
            raw_fip = max(1.50, min(raw_fip, 9.00))
            weight  = min(ip / RECENT_REGRESSION_IP, 1.0)
            result[r["name"]] = round(
                raw_fip * weight + FALLBACK_ERA_EST * (1.0 - weight), 2
            )

    with open(cache_file, "w") as f:
        json.dump(result, f)

    print(f"  Recent pitcher form loaded for {len(result)} pitchers")
    return result


def get_pitcher_recent_starts_ip(season, days=30):
    """
    Pull starting pitcher stats for the last {days} days.
    Returns {pitcher_name: {"avg_ip": float, "gs": int}} for starters with GS >= 1.

    Used to blend actual recent IP/start (50%) with the static 5-IP F5 assumption (50%).
    Only pitchers with GS >= 3 in the window are returned — others default to 5 IP.
    """
    from datetime import timedelta

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    cache_file = os.path.join(CACHE_DIR,
                              f"pitcher_recent_starts_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print(f"  Downloading pitcher recent starts IP ({days}-day) ...")

    url = f"{MLB_API}/stats"
    params = {
        "stats":      "byDateRange",
        "group":      "pitching",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "limit":      2000,
        "startDate":  start_date.strftime("%m/%d/%Y"),
        "endDate":    end_date.strftime("%m/%d/%Y"),
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Pitcher recent starts fetch failed — {e}")
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])
    result = {}

    for split in splits:
        s    = split.get("stat", {})
        name = split.get("player", {}).get("fullName", "")
        gs   = int(s.get("gamesStarted", 0))
        if not name or gs < 3:
            continue
        ip = _parse_ip(str(s.get("inningsPitched", "0.0")))
        if ip <= 0:
            continue
        result[name] = {"avg_ip": round(ip / gs, 2), "gs": gs}

    with open(cache_file, "w") as f:
        json.dump(result, f)

    print(f"  Recent starts IP loaded for {len(result)} starters")
    return result


def get_pitcher_starts_only_ip(season):
    """
    Fetch each pitcher's IP from games where they were the starter using the
    'sp' (Starter) sitCode split.  Unlike the season aggregate endpoint, this
    isolates starts-only innings and eliminates the dual-role bug where a
    pitcher with 1 start but 20 relief innings would produce 21 IP / 1 GS = 21.

    Returns {pitcher_name: {"ip": float, "gs": int}}.
    """
    cache_file = os.path.join(CACHE_DIR, f"pitcher_starts_ip_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cached = json.load(f)
        if cached:
            return cached
        os.remove(cache_file)

    print(f"  Downloading pitcher starts-only IP ({season}) from MLB Stats API ...")

    url = f"{MLB_API}/stats"
    params = {
        "stats":      "statSplits",
        "group":      "pitching",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "sitCodes":   "sp",
        "limit":      2000,
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Pitcher starts-only IP fetch failed ({season}) — {e}")
        return {}

    result = {}
    for group in data.get("stats", []):
        all_splits = group.get("splits", []) + group.get("splitsTiedWithLimit", [])
        for split in all_splits:
            name = split.get("player", {}).get("fullName", "")
            s    = split.get("stat", {})
            if not name:
                continue
            ip = _parse_ip(str(s.get("inningsPitched", "0.0")))
            gs = int(s.get("gamesStarted", 0))
            if ip > 0 and gs >= 1:
                result[name] = {"ip": round(ip, 1), "gs": gs}

    with open(cache_file, "w") as f:
        json.dump(result, f)

    print(f"  Starts-only IP loaded for {len(result)} starters ({season})")
    return result


def get_team_recent_form(season, days=30):
    """
    Pull team batting stats for the last {days} days.
    Returns {team_name: recent_wrc_plus} where 100 = league average.

    Minimum 100 PA in the window required to include a team.
    """
    from datetime import timedelta

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    cache_file = os.path.join(CACHE_DIR,
                              f"team_recent_{season}_{date.today()}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print(f"  Downloading team recent batting form ({days}-day) ...")

    url = f"{MLB_API}/teams/stats"
    params = {
        "stats":     "byDateRange",
        "group":     "hitting",
        "season":    season,
        "sportId":   1,
        "gameType":  "R",
        "startDate": start_date.strftime("%m/%d/%Y"),
        "endDate":   end_date.strftime("%m/%d/%Y"),
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Team recent form fetch failed — {e}")
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])

    try:
        from model import PARK_FACTORS
    except ImportError:
        PARK_FACTORS = {}

    rows = []
    for split in splits:
        s         = split.get("stat", {})
        team_name = split.get("team", {}).get("name", "")
        if not team_name:
            continue

        try:
            pa  = int(s.get("plateAppearances", 0))
            h   = int(s.get("hits",             0))
            d   = int(s.get("doubles",          0))
            t   = int(s.get("triples",          0))
            hr  = int(s.get("homeRuns",         0))
            bb  = int(s.get("baseOnBalls",      0))
            ibb = int(s.get("intentionalWalks", 0))
            hbp = int(s.get("hitByPitch",       0))
            sf  = int(s.get("sacFlies",         0))
            ab  = int(s.get("atBats",           0))
            r   = int(s.get("runs",             0))
        except (TypeError, ValueError):
            continue

        if pa < 100:
            continue

        woba = _compute_woba(h, d, t, hr, bb, ibb, hbp, sf, ab)
        rows.append({"team_name": team_name, "woba": woba, "pa": pa, "r": r})

    if not rows:
        with open(cache_file, "w") as f:
            json.dump({}, f)
        return {}

    total_pa    = sum(r["pa"] for r in rows)
    total_r     = sum(r["r"]  for r in rows)
    lg_r_per_pa = total_r / total_pa if total_pa > 0 else LG_R_PER_PA
    lg_woba     = sum(r["woba"] * r["pa"] for r in rows) / total_pa if total_pa > 0 else 0.320

    result = {}
    for r in rows:
        pf  = PARK_FACTORS.get(r["team_name"], 1.0)
        wrc = _woba_to_wrc_plus(r["woba"], lg_woba, lg_r_per_pa, pf)
        result[r["team_name"]] = wrc

    with open(cache_file, "w") as f:
        json.dump(result, f)

    print(f"  Recent team batting form loaded for {len(result)} teams")
    return result


def get_pitcher_velocity_trends(season):
    """
    Fetch average four-seam fastball velocity from Baseball Savant for the
    current and prior season and return the year-over-year delta per pitcher.

    Returns {pitcher_name: delta_mph} where:
      negative delta = velocity loss  (pitcher may be declining / injured)
      positive delta = velocity gain  (pitcher may have improved)

    Pitchers with fewer than 50 four-seamers in either season are excluded.
    Results are cached at the season level and refreshed every 7 days.
    Savant blocks GitHub Actions IPs, so if the fetch fails the existing
    committed cache is used as a fallback rather than returning empty.
    """
    cache_file = os.path.join(CACHE_DIR, f"pitcher_velo_{season}.json")

    existing = {}
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            existing = json.load(f)

    # Skip refresh if data is fresh (< 7 days old)
    if existing and os.path.exists(cache_file):
        age_days = (date.today() - date.fromtimestamp(os.path.getmtime(cache_file))).days
        if age_days < 7:
            return existing

    print("  Downloading pitcher velocity trends from Baseball Savant ...")

    def _fetch(yr):
        url = (
            "https://baseballsavant.mlb.com/statcast_search/csv"
            f"?all=true&hfPT=FF%7C&hfGT=R%7C&hfSea={yr}%7C"
            "&player_type=pitcher&group_by=name&min_pitches=50"
            "&sort_col=pitches&sort_order=desc"
        )
        try:
            resp = requests.get(url, timeout=30,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            resp.raise_for_status()
            text = resp.text.strip()
            if not text or text.startswith("<"):
                print(f"  WARNING: Savant velocity fetch returned unexpected response ({yr})")
                return {}
            reader = csv.DictReader(io.StringIO(text))
            out = {}
            for p in reader:
                name  = (p.get("player_name") or "").strip()
                speed = p.get("velocity")
                if name and speed:
                    try:
                        if ", " in name:
                            last, first = name.split(", ", 1)
                            name = f"{first} {last}"
                        out[name] = float(speed)
                    except ValueError:
                        pass
            return out
        except Exception as e:
            print(f"  WARNING: Savant velocity fetch failed ({yr}) — {e}")
            return {}

    current = _fetch(season)
    prior   = _fetch(season - 1)

    if not current:
        if existing:
            print(f"  Velocity fetch failed — using existing cache ({len(existing)} pitchers)")
            return existing
        return {}

    result = {}
    for name, curr_velo in current.items():
        if name in prior:
            result[name] = {
                "current": round(curr_velo, 1),
                "prior":   round(prior[name], 1),
                "delta":   round(curr_velo - prior[name], 1),
            }

    with open(cache_file, "w") as f:
        json.dump(result, f)

    declines = sum(1 for d in result.values() if d["delta"] <= -1.0)
    print(f"  Velocity trends: {len(result)} pitchers matched "
          f"({declines} with 1+ mph decline)")
    return result


def _parse_ip(ip_str):
    """
    Convert MLB's innings pitched format to a decimal.
    MLB uses .1 = 1/3 inning, .2 = 2/3 inning.
    e.g. '6.2' -> 6.667
    """
    try:
        parts = str(ip_str).split(".")
        full  = int(parts[0])
        third = int(parts[1]) if len(parts) > 1 else 0
        return full + third / 3.0
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# Team batting stats
# --------------------------------------------------------------------------- #

def get_team_batting_stats(season):
    """
    Pull team batting stats from the MLB Stats API.
    Computes true wRC+ using FanGraphs linear weights, park-adjusted.
    Returns a DataFrame with: TeamName, wRC+, K_pct, PA, and raw counting stats.
    """
    import pandas as pd

    cache_file = os.path.join(CACHE_DIR, f"team_batting_wrc_{season}_{date.today()}.csv")

    if os.path.exists(cache_file):
        print("  (Using cached team batting data from today)")
        return pd.read_csv(cache_file)

    print("  Downloading team batting stats from MLB Stats API ...")

    url = f"{MLB_API}/teams/stats"
    params = {
        "stats":    "season",
        "group":    "hitting",
        "season":   season,
        "sportId":  1,
        "gameType": "R",
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: MLB API team batting fetch failed — {e}")
        return pd.DataFrame()

    splits = data.get("stats", [{}])[0].get("splits", [])

    rows = []
    for split in splits:
        s    = split.get("stat", {})
        team = split.get("team", {})

        try:
            h   = int(s.get("hits",              0))
            d   = int(s.get("doubles",            0))
            t   = int(s.get("triples",            0))
            hr  = int(s.get("homeRuns",           0))
            bb  = int(s.get("baseOnBalls",        0))
            ibb = int(s.get("intentionalWalks",   0))
            hbp = int(s.get("hitByPitch",         0))
            sf  = int(s.get("sacFlies",           0))
            ab  = int(s.get("atBats",             0))
            pa  = int(s.get("plateAppearances",   0))
            r   = int(s.get("runs",               0))
            so  = int(s.get("strikeOuts",         0))
        except (TypeError, ValueError):
            continue

        if pa < 1:
            continue

        woba  = _compute_woba(h, d, t, hr, bb, ibb, hbp, sf, ab)
        k_pct = round(so / pa, 4) if pa > 0 else 0.22

        rows.append({
            "Team":     team.get("abbreviation", ""),
            "TeamName": team.get("name", ""),
            "PA":       pa,
            "R":        r,
            "wOBA":     woba,
            "K_pct":    k_pct,
        })

    if not rows:
        print("  WARNING: No team batting data returned.")
        return pd.DataFrame()

    # League averages across all teams
    total_pa    = sum(r["PA"] for r in rows)
    total_r     = sum(r["R"]  for r in rows)
    lg_r_per_pa = total_r / total_pa if total_pa > 0 else LG_R_PER_PA

    total_woba_pa = sum(r["wOBA"] * r["PA"] for r in rows)
    lg_woba       = total_woba_pa / total_pa if total_pa > 0 else 0.320

    # Import park factors for park adjustment
    try:
        from model import PARK_FACTORS
    except ImportError:
        PARK_FACTORS = {}

    for r in rows:
        pf      = PARK_FACTORS.get(r["TeamName"], 1.0)
        r["wRC+"] = _woba_to_wrc_plus(r["wOBA"], lg_woba, lg_r_per_pa, pf)

    import pandas as pd
    df = pd.DataFrame(rows)
    df.to_csv(cache_file, index=False)

    wrc_vals = sorted([(r["TeamName"].split()[-1], r["wRC+"]) for r in rows], key=lambda x: x[1])
    print(f"  Loaded {len(df)} teams  |  lg wOBA: {lg_woba:.3f}  "
          f"|  wRC+ range: {wrc_vals[0][1]} ({wrc_vals[0][0]}) → "
          f"{wrc_vals[-1][1]} ({wrc_vals[-1][0]})")
    return df


def get_team_defensive_stats(season):
    """
    Pull team pitching stats to compute unearned runs allowed per game.
    Unearned runs = total runs allowed - earned runs allowed.

    Returns a dict: { team_name: unearned_per_game }
    League average is ~0.35 unearned runs per team per game.
    Teams with higher rates have poor defense — their opponents score more.
    """
    cache_file = os.path.join(CACHE_DIR, f"team_defense_{season}_{date.today()}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    print(f"  Downloading team defensive stats ({season}) from MLB Stats API ...")

    url = f"{MLB_API}/teams/stats"
    params = {
        "stats":    "season",
        "group":    "pitching",
        "season":   season,
        "sportId":  1,
        "gameType": "R",
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        splits = resp.json().get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        print(f"  WARNING: MLB API team defense fetch failed — {e}")
        return {}

    result = {}
    total_unearned = 0
    total_games    = 0

    for split in splits:
        s    = split.get("stat", {})
        team = split.get("team", {})
        name = team.get("name", "")
        if not name:
            continue

        try:
            runs         = int(s.get("runs", 0) or 0)
            earned_runs  = int(s.get("earnedRuns", 0) or 0)
            games        = int(s.get("gamesPlayed", 0) or 0)
        except (TypeError, ValueError):
            continue

        if games < 1:
            continue

        unearned         = max(0, runs - earned_runs)
        unearned_per_game = round(unearned / games, 4)
        result[name]      = unearned_per_game
        total_unearned   += unearned
        total_games      += games

    lg_avg = round(total_unearned / total_games, 4) if total_games > 0 else 0.35
    result["__league_avg__"] = lg_avg

    with open(cache_file, "w") as f:
        json.dump(result, f)

    print(f"  Loaded defensive stats for {len(result)-1} teams  |  "
          f"League avg unearned/game: {lg_avg:.3f}")
    return result
