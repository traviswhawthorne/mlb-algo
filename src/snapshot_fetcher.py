"""
Snapshot Fetcher
================
Point-in-time stat fetchers for backtesting.

All functions return cumulative stats from season opening day through `snap_date`,
eliminating look-ahead bias.  A game on July 31 uses only data through the
nearest preceding weekly snapshot (~July 27).

Cache scheme: data/snap_{type}_{season}_{snap_date}.{ext}
"""

import os
import sys
import json
import requests
import pandas as pd
from datetime import date

_SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SRC)

from stats_fetcher import (
    _parse_ip, _compute_woba, _woba_to_wrc_plus,
    FALLBACK_ERA_EST, LG_R_PER_PA,
)

CACHE_DIR = os.path.join(os.path.dirname(_SRC), "data")
os.makedirs(CACHE_DIR, exist_ok=True)
MLB_API = "https://statsapi.mlb.com/api/v1"

_REGRESSION_IP       = 60.0
_SPLIT_REGRESSION_IP = 40.0


# ── helpers ──────────────────────────────────────────────────────────────────

def _season_start(season):
    return f"04/01/{season}"

def _fmt(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%m/%d/%Y")

def _jcache(tag, season, snap_date):
    return os.path.join(CACHE_DIR, f"snap_{tag}_{season}_{snap_date}.json")

def _cvcache(tag, season, snap_date):
    return os.path.join(CACHE_DIR, f"snap_{tag}_{season}_{snap_date}.csv")

def _load_j(path):
    if os.path.exists(path):
        with open(path) as f:
            d = json.load(f)
        if d:
            return d
    return None

def _save_j(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── 1. Pitcher stats thru date ───────────────────────────────────────────────

def get_pitcher_stats_thru(season, snap_date):
    """
    Cumulative pitcher stats from season start through snap_date.
    Returns DataFrame (Name, Team, GS, IP, ERA, K%, BB%, raw_fip, ERA_est).
    """
    csv_path = _cvcache("pitchers", season, snap_date)
    if os.path.exists(csv_path):
        return pd.read_csv(csv_path)

    url    = f"{MLB_API}/stats"
    params = {
        "stats":      "byDateRange",
        "group":      "pitching",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "limit":      2000,
        "startDate":  _season_start(season),
        "endDate":    _fmt(snap_date),
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        splits = resp.json().get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        print(f"  WARNING: pitcher stats thru {snap_date} — {e}")
        return pd.DataFrame()

    rows = []
    for s in splits:
        stat = s.get("stat", {})
        name = s.get("player", {}).get("fullName", "Unknown")
        team = s.get("team",   {}).get("name",     "")
        ip   = _parse_ip(str(stat.get("inningsPitched", "0.0")))
        if ip < 3.0:
            continue
        k   = int(stat.get("strikeOuts",  0))
        bb  = int(stat.get("baseOnBalls", 0))
        hr  = int(stat.get("homeRuns",    0))
        hbp = int(stat.get("hitBatsmen",  0))
        bf  = int(stat.get("battersFaced", 0))
        gs  = int(stat.get("gamesStarted", 0))
        try:
            era = float(str(stat.get("era", "4.50")))
        except ValueError:
            era = 4.50
        fip_num = (13 * hr) + (3 * (bb + hbp)) - (2 * k)
        rows.append({
            "Name": name, "Team": team, "GS": gs, "IP": round(ip, 1),
            "ERA": era,
            "K%": round(k / bf, 4) if bf else 0.20,
            "BB%": round(bb / bf, 4) if bf else 0.08,
            "_fn": fip_num, "_ip": ip,
        })

    if not rows:
        return pd.DataFrame()

    t_ip  = sum(r["_ip"] for r in rows)
    t_fn  = sum(r["_fn"] for r in rows)
    lg_era = sum(r["ERA"] * r["_ip"] for r in rows) / max(t_ip, 1)
    const  = lg_era - (t_fn / max(t_ip, 1))

    for r in rows:
        ip = r["_ip"]
        rf = max(1.5, min((r["_fn"] / ip) + const, 9.0)) if ip else FALLBACK_ERA_EST
        w  = min(ip / _REGRESSION_IP, 1.0)
        r["raw_fip"] = round(rf, 2)
        r["ERA_est"] = round(rf * w + FALLBACK_ERA_EST * (1 - w), 2)
        del r["_fn"], r["_ip"]

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    return df


# ── 2. Team batting thru date ────────────────────────────────────────────────

def get_team_batting_thru(season, snap_date):
    """
    Cumulative team batting stats through snap_date.
    Returns DataFrame (Team, TeamName, PA, R, wOBA, K_pct, wRC+).
    """
    csv_path = _cvcache("batting", season, snap_date)
    if os.path.exists(csv_path):
        return pd.read_csv(csv_path)

    try:
        from model import PARK_FACTORS
    except ImportError:
        PARK_FACTORS = {}

    url    = f"{MLB_API}/teams/stats"
    params = {
        "stats":     "byDateRange",
        "group":     "hitting",
        "season":    season,
        "sportId":   1,
        "gameType":  "R",
        "startDate": _season_start(season),
        "endDate":   _fmt(snap_date),
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        splits = resp.json().get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        print(f"  WARNING: team batting thru {snap_date} — {e}")
        return pd.DataFrame()

    rows = []
    for s in splits:
        stat = s.get("stat", {})
        team = s.get("team", {})
        try:
            h   = int(stat.get("hits",            0))
            dd  = int(stat.get("doubles",          0))
            t   = int(stat.get("triples",          0))
            hr  = int(stat.get("homeRuns",         0))
            bb  = int(stat.get("baseOnBalls",      0))
            ibb = int(stat.get("intentionalWalks", 0))
            hbp = int(stat.get("hitByPitch",       0))
            sf  = int(stat.get("sacFlies",         0))
            ab  = int(stat.get("atBats",           0))
            pa  = int(stat.get("plateAppearances", 0))
            r   = int(stat.get("runs",             0))
            so  = int(stat.get("strikeOuts",       0))
        except (TypeError, ValueError):
            continue
        if pa < 1:
            continue
        woba = _compute_woba(h, dd, t, hr, bb, ibb, hbp, sf, ab)
        rows.append({
            "Team":     team.get("abbreviation", ""),
            "TeamName": team.get("name", ""),
            "PA": pa, "R": r, "wOBA": woba,
            "K_pct": round(so / pa, 4) if pa else 0.22,
        })

    if not rows:
        return pd.DataFrame()

    t_pa = sum(r["PA"] for r in rows)
    t_r  = sum(r["R"]  for r in rows)
    lg_r = t_r / t_pa if t_pa else LG_R_PER_PA
    lg_w = sum(r["wOBA"] * r["PA"] for r in rows) / t_pa if t_pa else 0.320
    for r in rows:
        pf = PARK_FACTORS.get(r["TeamName"], 1.0)
        r["wRC+"] = _woba_to_wrc_plus(r["wOBA"], lg_w, lg_r, pf)

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    return df


# ── 3. Team split stats thru date ────────────────────────────────────────────

def get_team_split_stats_thru(season, snap_date):
    """
    Team batting splits (vs_R / vs_L wRC+) through snap_date.
    Returns dict: {team_name: {"vs_R": int, "vs_L": int}}
    """
    cache = _jcache("team_splits", season, snap_date)
    cached = _load_j(cache)
    if cached is not None:
        return cached

    try:
        from model import PARK_FACTORS
    except ImportError:
        PARK_FACTORS = {}

    raw = {}
    for sit_code, label in [("vr", "vs_R"), ("vl", "vs_L")]:
        url    = f"{MLB_API}/teams/stats"
        params = {
            "stats":     "statSplits",
            "group":     "hitting",
            "season":    season,
            "sportId":   1,
            "gameType":  "R",
            "sitCodes":  sit_code,
            "startDate": _season_start(season),
            "endDate":   _fmt(snap_date),
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WARNING: team split {sit_code} thru {snap_date} — {e}")
            continue
        for group in data.get("stats", []):
            for s in group.get("splits", []):
                tname = s.get("team", {}).get("name", "")
                stat  = s.get("stat", {})
                if not tname:
                    continue
                try:
                    h   = int(stat.get("hits",            0))
                    dd  = int(stat.get("doubles",          0))
                    t2  = int(stat.get("triples",          0))
                    hr  = int(stat.get("homeRuns",         0))
                    bb  = int(stat.get("baseOnBalls",      0))
                    ibb = int(stat.get("intentionalWalks", 0))
                    hbp = int(stat.get("hitByPitch",       0))
                    sf  = int(stat.get("sacFlies",         0))
                    ab  = int(stat.get("atBats",           0))
                    pa  = int(stat.get("plateAppearances", 0))
                    r   = int(stat.get("runs",             0))
                except (TypeError, ValueError):
                    continue
                if pa < 1:
                    continue
                woba = _compute_woba(h, dd, t2, hr, bb, ibb, hbp, sf, ab)
                raw.setdefault(tname, {})[label] = {"woba": woba, "pa": pa, "r": r}

    result = {}
    for sit_label in ["vs_R", "vs_L"]:
        entries = [(tn, v[sit_label]) for tn, v in raw.items() if sit_label in v]
        if not entries:
            continue
        t_pa = sum(e["pa"] for _, e in entries)
        t_r  = sum(e["r"]  for _, e in entries)
        lg_r = t_r / t_pa if t_pa else LG_R_PER_PA
        lg_w = sum(e["woba"] * e["pa"] for _, e in entries) / t_pa if t_pa else 0.320
        for tn, entry in entries:
            pf  = PARK_FACTORS.get(tn, 1.0)
            wrc = _woba_to_wrc_plus(entry["woba"], lg_w, lg_r, pf)
            result.setdefault(tn, {})[sit_label] = max(50, min(wrc, 160))

    _save_j(cache, result)
    return result


# ── 4. Pitcher platoon splits thru date ──────────────────────────────────────

def get_pitcher_split_stats_thru(season, snap_date):
    """
    Pitcher platoon ERA splits (vs_L / vs_R) through snap_date.
    Returns dict: {name: {"vs_L": era_est, "vs_R": era_est}}
    """
    cache = _jcache("pitcher_splits", season, snap_date)
    cached = _load_j(cache)
    if cached is not None:
        return cached

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
            "startDate":  _season_start(season),
            "endDate":    _fmt(snap_date),
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WARNING: pitcher split {sit_code} thru {snap_date} — {e}")
            continue
        for group in data.get("stats", []):
            for s in (group.get("splits", []) + group.get("splitsTiedWithLimit", [])):
                name = s.get("player", {}).get("fullName", "")
                stat = s.get("stat",   {})
                if not name:
                    continue
                ip = _parse_ip(str(stat.get("inningsPitched", "0.0")))
                if ip < 3:
                    continue
                try:
                    era = float(stat.get("era") or FALLBACK_ERA_EST)
                    era = max(1.5, min(era, 9.0))
                except (TypeError, ValueError):
                    era = FALLBACK_ERA_EST
                w = min(ip / _SPLIT_REGRESSION_IP, 1.0)
                result.setdefault(name, {})[label] = round(
                    era * w + FALLBACK_ERA_EST * (1 - w), 2
                )

    _save_j(cache, result)
    return result


# ── 5. Pitcher home/away splits thru date ────────────────────────────────────

def get_pitcher_ha_stats_thru(season, snap_date):
    """
    Pitcher home/away ERA splits through snap_date.
    Returns dict: {name: {"home": era_est, "away": era_est}}
    """
    cache = _jcache("pitcher_ha", season, snap_date)
    cached = _load_j(cache)
    if cached is not None:
        return cached

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
            "startDate":  _season_start(season),
            "endDate":    _fmt(snap_date),
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WARNING: pitcher H/A {sit_code} thru {snap_date} — {e}")
            continue
        for group in data.get("stats", []):
            for s in (group.get("splits", []) + group.get("splitsTiedWithLimit", [])):
                name = s.get("player", {}).get("fullName", "")
                stat = s.get("stat",   {})
                if not name:
                    continue
                ip = _parse_ip(str(stat.get("inningsPitched", "0.0")))
                if ip < 3:
                    continue
                try:
                    era = float(stat.get("era") or FALLBACK_ERA_EST)
                    era = max(1.5, min(era, 9.0))
                except (TypeError, ValueError):
                    era = FALLBACK_ERA_EST
                w = min(ip / _SPLIT_REGRESSION_IP, 1.0)
                result.setdefault(name, {})[label] = round(
                    era * w + FALLBACK_ERA_EST * (1 - w), 2
                )

    _save_j(cache, result)
    return result


# ── 6. Team defense thru date ────────────────────────────────────────────────

def get_team_defense_thru(season, snap_date):
    """
    Team unearned runs allowed per game through snap_date.
    Returns dict: {team_name: float, "__league_avg__": float}
    """
    cache = _jcache("defense", season, snap_date)
    cached = _load_j(cache)
    if cached is not None:
        return cached

    url    = f"{MLB_API}/teams/stats"
    params = {
        "stats":     "byDateRange",
        "group":     "pitching",
        "season":    season,
        "sportId":   1,
        "gameType":  "R",
        "startDate": _season_start(season),
        "endDate":   _fmt(snap_date),
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        splits = resp.json().get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        print(f"  WARNING: team defense thru {snap_date} — {e}")
        return {}

    result = {}
    total_ue = 0
    total_g  = 0
    for s in splits:
        stat = s.get("stat", {})
        name = s.get("team", {}).get("name", "")
        if not name:
            continue
        try:
            runs   = int(stat.get("runs",        0) or 0)
            earned = int(stat.get("earnedRuns",  0) or 0)
            games  = int(stat.get("gamesPlayed", 0) or 0)
        except (TypeError, ValueError):
            continue
        if games < 1:
            continue
        ue = max(0, runs - earned)
        result[name] = round(ue / games, 4)
        total_ue += ue
        total_g  += games

    result["__league_avg__"] = round(total_ue / total_g, 4) if total_g else 0.35
    _save_j(cache, result)
    return result


# ── 7. Starts-only IP thru date (sitCodes=sp) ────────────────────────────────

def get_starts_ip_thru(season, snap_date):
    """
    Pitcher starts-only IP through snap_date via sitCodes=sp.
    Returns dict: {name: {"ip": float, "gs": int}}
    """
    cache = _jcache("starts_ip", season, snap_date)
    cached = _load_j(cache)
    if cached is not None:
        return cached

    url    = f"{MLB_API}/stats"
    params = {
        "stats":      "statSplits",
        "group":      "pitching",
        "season":     season,
        "playerPool": "all",
        "sportId":    1,
        "gameType":   "R",
        "sitCodes":   "sp",
        "limit":      2000,
        "startDate":  _season_start(season),
        "endDate":    _fmt(snap_date),
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: starts-only IP thru {snap_date} — {e}")
        return {}

    result = {}
    for group in data.get("stats", []):
        for s in (group.get("splits", []) + group.get("splitsTiedWithLimit", [])):
            name = s.get("player", {}).get("fullName", "")
            stat = s.get("stat",   {})
            if not name:
                continue
            ip = _parse_ip(str(stat.get("inningsPitched", "0.0")))
            gs = int(stat.get("gamesStarted", 0))
            if ip > 0 and gs >= 1:
                result[name] = {"ip": round(ip, 1), "gs": gs}

    _save_j(cache, result)
    return result


# ── 8. Bullpen stats (derived, no API call) ───────────────────────────────────

def get_bullpen_stats_thru(pitchers_df, pitcher_splits_thru):
    """
    Compute team bullpen ERA from snapshot pitcher DataFrame.
    Mirrors stats_fetcher.get_bullpen_stats() logic.
    Returns {team: {"overall": float, "vs_L": float, "vs_R": float}}
    """
    if pitchers_df is None or pitchers_df.empty:
        return {}

    relievers = pitchers_df[
        (pitchers_df["GS"] == 0) & (pitchers_df["IP"] >= 3.0)
    ].copy()
    if relievers.empty:
        return {}

    team_bullpen = {}
    for team_name, group in relievers.groupby("Team"):
        total_ip = group["IP"].sum()
        if total_ip < 10:
            continue
        raw_era = (group["ERA"] * group["IP"]).sum() / total_ip
        raw_era = max(1.5, min(raw_era, 9.0))
        w       = min(total_ip / 60.0, 1.0)
        overall = raw_era * w + FALLBACK_ERA_EST * (1 - w)

        vs_l_num = vs_l_den = vs_r_num = vs_r_den = 0.0
        for _, row in group.iterrows():
            name = row["Name"]
            ip   = row["IP"]
            sp   = pitcher_splits_thru.get(name, {})
            if "vs_L" in sp:
                vs_l_num += sp["vs_L"] * ip
                vs_l_den += ip
            if "vs_R" in sp:
                vs_r_num += sp["vs_R"] * ip
                vs_r_den += ip

        team_bullpen[team_name] = {
            "overall": round(overall, 2),
            "vs_L":    round(vs_l_num / vs_l_den, 2) if vs_l_den else round(overall, 2),
            "vs_R":    round(vs_r_num / vs_r_den, 2) if vs_r_den else round(overall, 2),
        }

    return team_bullpen
