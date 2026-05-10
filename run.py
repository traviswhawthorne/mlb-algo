#!/usr/bin/env python3
"""
MLB BETTING ALGORITHM
=====================
Run this every morning during the MLB season to get today's picks.

Usage (you only need to do this — nothing else):
  Double-click this file  OR  type in terminal:  py run.py

Output: MLB_Picks.xlsx opens automatically when done.

Setup (one-time):
  1. Get free Odds API key at: https://the-odds-api.com
  2. Open config.py and paste your key where it says YOUR_API_KEY_HERE
  3. Set your BANKROLL in config.py
"""

import sys
import os
import subprocess

# Add src/ to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from datetime import datetime
import pytz

# ---- Tee stdout to a log file ----
class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except (ValueError, OSError):
                pass
    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except (ValueError, OSError):
                pass

_log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_log_dir, exist_ok=True)
_log_path = os.path.join(_log_dir, f"run_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
_log_file = open(_log_path, "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_file)
print(f"Log: {_log_path}")

import config
from stats_fetcher   import (get_pitcher_stats, get_team_batting_stats,
                              get_bullpen_stats, get_batter_stats,
                              get_player_handedness, get_player_bat_side,
                              get_team_split_stats, get_pitcher_split_stats,
                              get_pitcher_home_away_stats,
                              get_rest_days, get_bullpen_usage,
                              get_pitcher_recent_form, get_team_recent_form,
                              get_pitcher_recent_starts_ip,
                              get_pitcher_starts_only_ip,
                              get_team_defensive_stats,
                              get_pitcher_velocity_trends)
from games_fetcher   import get_todays_games
from odds_fetcher    import get_mlb_odds, parse_odds
from weather_fetcher import get_weather
from umpire_fetcher  import get_umpire_tendencies, get_game_umpire, get_umpire_run_factor
from model           import (calculate_game_probability,
                              calculate_f5_probability,
                              calculate_over_probability,
                              calculate_runline_probabilities,
                              calculate_strikeout_projection)
from ev_calculator   import analyze_bet, prob_to_american_odds
from name_matcher    import (find_pitcher_stats, find_team_wrc_plus,
                              find_team_bullpen_era, adjust_wrc_for_lineup,
                              find_pitcher_hand, find_team_split_wrc,
                              find_pitcher_split_era, find_team_k_rate,
                              get_lineup_hand_pct, get_platoon_flag,
                              find_pitcher_ha_era, match_odds_game,
                              _normalize)
from output          import write_picks_to_excel


def _f5_starter_ip(pitcher_name, recent_starts):
    """
    Blended F5 starter IP: 50% static 5 IP + 50% recent avg IP/start.
    Requires GS >= 3 in recent window; otherwise returns 5.0.
    Capped at 5.0 since F5 is only 5 innings.
    """
    if not pitcher_name or pitcher_name == "TBD":
        return 5.0
    entry = recent_starts.get(pitcher_name)
    if entry is None or entry.get("gs", 0) < 3:
        return 5.0
    blended = 0.5 * 5.0 + 0.5 * entry["avg_ip"]
    return min(round(blended, 2), 5.0)



# ------------------------------------------------------------------ #
def _prior_weight(ip):
    """
    Piecewise linear weight for current-season stats vs prior-year stats.
    Returns the fraction to assign to the current year.

       0 IP →   0%  (100% prior)
      12 IP →  30%  (70% prior)
      24 IP →  60%  (40% prior)
      48 IP →  80%  (20% prior)
      96 IP → 100%  (fully trust current year)
    """
    if ip <= 0:  return 0.0
    if ip >= 96: return 1.0
    if ip <= 12: return 0.30 * ip / 12
    if ip <= 24: return 0.30 + 0.30 * (ip - 12) / 12
    if ip <= 48: return 0.60 + 0.20 * (ip - 24) / 24
    return        0.80 + 0.20 * (ip - 48) / 48


def _quality_bp_adj(unavail_list, team_bp_era, pitchers_df, decay,
                    base_per_arm=0.15, cap=0.45):
    """
    Recompute taxed-bullpen ERA adjustment, weighting each unavailable arm by quality.
    Losing a closer (low ERA) hurts more; losing a mop-up guy (high ERA) barely matters.
    """
    if not unavail_list:
        return 0.0
    total_adj = 0.0
    for arm_name, _ in unavail_list:
        arm_adj = base_per_arm  # default if we can't find the arm
        if pitchers_df is not None and not pitchers_df.empty:
            match = pitchers_df[
                (pitchers_df["GS"] == 0) &
                (pitchers_df["Name"].apply(_normalize) == _normalize(arm_name))
            ]
            if not match.empty:
                arm_era = float(match.iloc[0]["ERA"])
                # quality_multiplier > 1 → arm is better than team avg (losing a key arm)
                # quality_multiplier < 1 → arm is worse than team avg (mop-up, minimal impact)
                multiplier = max(0.3, min(2.0, team_bp_era / max(arm_era, 1.5)))
                arm_adj = base_per_arm * multiplier
        total_adj += arm_adj
    return round(min(total_adj, cap) * decay, 2)


def _team_prior_weight(pa):
    """
    Same blend curve as _prior_weight but scaled for team plate appearances.
    300 PA ≈ 15-game equivalent, 1200 PA ≈ 60-game, 2000 PA ≈ full season.
    """
    return _prior_weight(pa / 20.0)   # 20 PA per IP-equivalent unit


def _blend_pitcher_ha(current_ha, prior_ha, pitchers_df):
    """
    Blend current-year and prior-year pitcher home/away ERA splits by IP weight.
    Same curve as overall ERA: 20% current at 15 IP → 100% current at 100 IP.
    """
    merged = {}
    all_names = set(current_ha) | set(prior_ha)
    for name in all_names:
        curr  = current_ha.get(name, {})
        prior = prior_ha.get(name, {})

        ip = 0.0
        if pitchers_df is not None and not pitchers_df.empty:
            match = pitchers_df[pitchers_df["Name"] == name]
            if not match.empty:
                try:
                    ip = float(match.iloc[0]["IP"])
                except (TypeError, ValueError):
                    ip = 0.0
        w = _prior_weight(ip)

        blended = {}
        for side in ("home", "away"):
            c = curr.get(side)
            p = prior.get(side)
            if c is not None and p is not None:
                blended[side] = round(w * c + (1 - w) * p, 2)
            elif c is not None:
                blended[side] = c
            elif p is not None:
                blended[side] = p
        if blended:
            merged[name] = blended
    return merged


def _blend_pitcher_splits(current_splits, prior_splits, pitchers_df):
    """
    Blend pitcher vs-L / vs-R ERA splits between current and prior year.
    Uses the same IP-based weight curve as overall ERA blending.
    Pitchers with only prior-year split data are included at full prior weight.
    """
    import pandas as pd
    merged = {}

    all_names = set(current_splits) | set(prior_splits)
    for name in all_names:
        curr  = current_splits.get(name, {})
        prior = prior_splits.get(name, {})

        # Look up current-year IP to determine blend weight
        ip = 0.0
        if pitchers_df is not None and not pitchers_df.empty:
            match = pitchers_df[pitchers_df["Name"] == name]
            if not match.empty:
                try:
                    ip = float(match.iloc[0]["IP"])
                except (TypeError, ValueError):
                    ip = 0.0
        w = _prior_weight(ip)

        blended = {}
        for side in ("vs_L", "vs_R"):
            c = curr.get(side)
            p = prior.get(side)
            if c is not None and p is not None:
                blended[side] = round(w * c + (1 - w) * p, 2)
            elif c is not None:
                blended[side] = c      # rookie / no prior data
            elif p is not None:
                blended[side] = p      # no current data yet, use prior
        if blended:
            merged[name] = blended

    return merged


def _rebuild_era_est(pitchers_df, prior_pitchers_df):
    """
    Replace league-average FIP regression with player-specific prior-year regression.

    stats_fetcher blends: ERA_est = raw_fip * weight + 4.25 * (1-weight)
    We replace 4.25 with the pitcher's own prior-year ERA_est, so early-season FIP
    regresses toward what we know about that specific pitcher, not a generic average.

    Pitchers with no prior year data keep the league-average regression (unchanged).
    """
    import pandas as pd

    if pitchers_df is None or pitchers_df.empty:
        return pitchers_df
    if prior_pitchers_df is None or prior_pitchers_df.empty:
        return pitchers_df
    if "raw_fip" not in pitchers_df.columns:
        return pitchers_df   # old cache without raw_fip — fall back gracefully

    updated = pitchers_df.copy()

    for idx, row in updated.iterrows():
        ip = float(row.get("IP", 0) or 0)
        raw_fip = float(row.get("raw_fip", row.get("ERA_est", 4.25)))
        weight = _prior_weight(ip)

        prior_era, _ = find_pitcher_stats(row["Name"], prior_pitchers_df)
        updated.at[idx, "ERA_est"] = round(
            raw_fip * weight + prior_era * (1.0 - weight), 2
        )

    return updated


def _pitcher_ip_per_start(starts_only_dict):
    """
    Return {name: avg_ip_per_start} from starts-only IP data fetched via sitCodes=sp.

    The old approach (total_season_IP / GS) was broken for dual-role pitchers:
    a reliever with 1 spot start and 20 relief innings produced 21 IP/start.
    The new source isolates IP earned specifically in starting appearances.
    """
    result = {}
    for name, data in (starts_only_dict or {}).items():
        gs = data.get("gs", 0)
        ip = data.get("ip", 0.0)
        if gs >= 1 and ip > 0:
            result[name] = round(ip / gs, 3)
    return result


def _blend_ip_per_start(current_dict, prior_dict, pitchers_df):
    """
    Blend current and prior year avg IP/start per pitcher.

    Blend curve (by current-year GS):
      0 GS  → 100% prior year
      3 GS  → 50% current / 50% prior
      6+ GS → 100% current

    Fallback chain:
      - Both years available: blend by curve
      - Only current available: use current
      - Only prior available: use prior (pitcher hasn't started yet this year)
      - Neither: return None → ERA-formula fallback in model.py
    """
    all_names = set(current_dict) | set(prior_dict)
    result = {}
    for name in all_names:
        gs = 0
        if pitchers_df is not None and not pitchers_df.empty:
            match = pitchers_df[pitchers_df["Name"] == name]
            if not match.empty:
                try:
                    gs = int(match.iloc[0].get("GS", 0) or 0)
                except (TypeError, ValueError):
                    gs = 0

        w_current = min(gs / 6.0, 1.0)
        w_prior   = 1.0 - w_current

        curr  = current_dict.get(name)
        prior = prior_dict.get(name)

        if curr is not None and prior is not None:
            result[name] = round(w_current * curr + w_prior * prior, 3)
        elif curr is not None:
            result[name] = round(curr, 3)
        elif prior is not None:
            result[name] = round(prior, 3)
        # else: no data — omit so model falls back to ERA formula

    return result


def _blend_team_splits(current_splits, prior_splits, team_batting_df):
    """
    Blend team vs-R / vs-L wRC+ splits between current and prior year.
    Uses PA-based weight curve consistent with overall wRC+ blending.
    """
    merged = {}
    all_teams = set(current_splits) | set(prior_splits)
    for team in all_teams:
        curr  = current_splits.get(team, {})
        prior = prior_splits.get(team, {})
        pa    = _get_team_pa(team, team_batting_df)
        w     = _team_prior_weight(pa)

        blended = {}
        for side in ("vs_R", "vs_L"):
            c = curr.get(side)
            p = prior.get(side)
            if c is not None and p is not None:
                blended[side] = round(w * c + (1 - w) * p)
            elif c is not None:
                blended[side] = c
            elif p is not None:
                blended[side] = p
        if blended:
            merged[team] = blended

    return merged


def _get_team_pa(team_name, team_batting_df):
    """Look up a team's season plate appearances from the batting DataFrame."""
    if team_batting_df is None or team_batting_df.empty or "PA" not in team_batting_df.columns:
        return 0
    match = team_batting_df[team_batting_df["TeamName"] == team_name]
    if match.empty:
        # try last-word match
        last = team_name.split()[-1]
        match = team_batting_df[
            team_batting_df["TeamName"].str.contains(last, case=False, na=False)
        ]
    if match.empty:
        return 0
    try:
        return int(match.iloc[0]["PA"])
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ #
def _is_priority_bet(bet):
    """
    4-year backtest filter (2018/2019/2021/2025 — 1,520 priority bets):
      - 60%+ model prob:  +7.6% to +18.2% ROI across all years
      - EV >= 5%:         5-10% bucket is positive at 60%+ (was negative unfiltered)
      - Skip odds -200+:  heavy chalk kills edge in 3 of 4 years
      - Totals: Overs only — structurally aligned with high-confidence picks;
                Unders at 60%+ are tiny samples and inconsistent
    """
    ev     = bet.get("ev", 0)
    odds   = bet.get("book_odds")
    prob   = bet.get("model_prob", 0)
    market = bet.get("market", "")
    team   = bet.get("team", "")

    if prob < 0.60:                       return False
    if ev < 0.05:                         return False
    if odds is not None and odds <= -200: return False
    if market in ("Total", "F5 Total") and not str(team).startswith("Over"):
        return False
    return True


def _is_fade_bet(bet, book_total_line):
    """
    Backtest fade signal (2018/2019/2021 — 101 bets, +18.7% flip ROI):
    When the model bets Over on a game with a 9.0–10.5 total, the Under
    has historically won 61.4% of the time. Flag for monitoring — not yet
    enough 2026 live data to act on, but tracking it here to build the sample.

    Excludes Rockies home games: Coors Field totals run 11–14+, so a 10-total
    there is a suppressed line (ace/weather), not the same pattern. Zero Coors
    games appeared in the backtest fade sample.
    """
    if _is_priority_bet(bet):
        return False
    if bet.get("market") not in ("Total", "F5 Total"):
        return False
    if not str(bet.get("team", "")).startswith("Over"):
        return False
    if book_total_line is None:
        return False
    if "colorado" in str(bet.get("home_team", "")).lower():
        return False
    return 9.0 <= book_total_line <= 10.5


# ------------------------------------------------------------------ #
def _et_time(utc_str):
    """Convert MLB API UTC time string to Pacific time display string."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        pt = dt.astimezone(pytz.timezone("America/Los_Angeles"))
        hour = pt.hour % 12 or 12
        am_pm = "AM" if pt.hour < 12 else "PM"
        return f"{hour}:{pt.minute:02d} {am_pm} PT"
    except Exception:
        return utc_str


# ------------------------------------------------------------------ #
def main():
    from datetime import date

    print()
    print("=" * 62)
    print("  MLB BETTING ALGORITHM")
    print(f"  {date.today().strftime('%A, %B %d, %Y')}")
    print("=" * 62)

    # ---- 1. Load all stats ----
    print("\n[1/4] Loading stats ...")
    try:
        pitchers_df       = get_pitcher_stats(config.SEASON)
        prior_pitchers_df = get_pitcher_stats(config.SEASON - 1)
        team_batting_df   = get_team_batting_stats(config.SEASON)
        prior_batting_df  = get_team_batting_stats(config.SEASON - 1)
        defense_dict      = get_team_defensive_stats(config.SEASON)
        prior_defense_dict = get_team_defensive_stats(config.SEASON - 1)
        bullpen_dict      = get_bullpen_stats(config.SEASON, pitchers_df)
        batter_ops        = get_batter_stats(config.SEASON)
        hand_dict         = get_player_handedness(config.SEASON)
        bat_side_dict     = get_player_bat_side(config.SEASON)
        split_dict        = get_team_split_stats(config.SEASON)
        pitcher_split_dict       = get_pitcher_split_stats(config.SEASON)
        prior_pitcher_split_dict = get_pitcher_split_stats(config.SEASON - 1)
        prior_split_dict         = get_team_split_stats(config.SEASON - 1)
        pitcher_ha_dict          = get_pitcher_home_away_stats(config.SEASON)
        prior_pitcher_ha_dict    = get_pitcher_home_away_stats(config.SEASON - 1)
        rest_dict               = get_rest_days()
        bullpen_usage           = get_bullpen_usage()
        pitcher_recent_form     = get_pitcher_recent_form(config.SEASON, days=30)
        pitcher_recent_starts   = get_pitcher_recent_starts_ip(config.SEASON, days=30)
        team_recent_form        = get_team_recent_form(config.SEASON, days=14)
        velocity_trends         = get_pitcher_velocity_trends(config.SEASON)
        umpire_tendencies       = get_umpire_tendencies(config.SEASON)
        # Blend current-year and prior-year split dicts using IP/PA weights
        pitcher_split_dict = _blend_pitcher_splits(
            pitcher_split_dict, prior_pitcher_split_dict, pitchers_df
        )
        split_dict = _blend_team_splits(
            split_dict, prior_split_dict, team_batting_df
        )
        pitcher_ha_dict = _blend_pitcher_ha(
            pitcher_ha_dict, prior_pitcher_ha_dict, pitchers_df
        )
        starts_only_ip_current = get_pitcher_starts_only_ip(config.SEASON)
        starts_only_ip_prior   = get_pitcher_starts_only_ip(config.SEASON - 1)
        ip_per_start_dict = _blend_ip_per_start(
            _pitcher_ip_per_start(starts_only_ip_current),
            _pitcher_ip_per_start(starts_only_ip_prior),
            pitchers_df,
        )
        # Rebuild ERA_est: replace league-avg FIP regression with player-specific 2025 prior
        pitchers_df = _rebuild_era_est(pitchers_df, prior_pitchers_df)
        print(f"  {len(pitchers_df)} pitchers | {len(team_batting_df)} teams | "
              f"{len(bullpen_dict)} bullpens | {len(batter_ops)} batters | "
              f"{len(pitcher_split_dict)} pitcher splits (blended) | "
              f"{len(rest_dict)} rest records | "
              f"{len(pitcher_recent_form)} recent pitchers | {len(umpire_tendencies)} umpires")
    except Exception as e:
        print(f"  WARNING: Stats load failed — {e}")
        print("  Using league-average fallback values.")
        pitchers_df        = None
        team_batting_df    = None
        bullpen_dict       = {}
        batter_ops         = {}
        hand_dict          = {}
        bat_side_dict      = {}
        split_dict         = {}
        pitcher_split_dict  = {}
        rest_dict           = {}
        bullpen_usage            = {}
        pitcher_recent_form      = {}
        team_recent_form         = {}
        umpire_tendencies        = {}
        prior_pitchers_df        = None
        prior_batting_df         = None
        prior_pitcher_split_dict = {}
        prior_split_dict         = {}
        pitcher_ha_dict          = {}
        prior_pitcher_ha_dict    = {}
        defense_dict             = {}
        prior_defense_dict       = {}
        ip_per_start_dict        = {}
        pitcher_recent_starts    = {}
        velocity_trends          = {}

    # ---- 2. Get today's games (fall back to tomorrow if today's are all done) ----
    print("\n[2/4] Fetching today's schedule from MLB ...")
    from datetime import timedelta
    try:
        # GAME_DATE is set by the coordinator (PT date) to avoid UTC midnight rollover
        # issues on GitHub Actions for late west coast games.
        _forced_date = os.environ.get("GAME_DATE")
        games      = get_todays_games(_forced_date)
        games_date = date.fromisoformat(_forced_date) if _forced_date else date.today()
        game_label = "today"
        if not games and not _forced_date:
            games_date   = date.today() + timedelta(days=1)
            tomorrow_str = games_date.strftime("%Y-%m-%d")
            print(f"  No upcoming games today — checking tomorrow ({tomorrow_str}) ...")
            games      = get_todays_games(tomorrow_str)
            game_label = f"tomorrow ({tomorrow_str})"
    except Exception as e:
        print(f"  ERROR: Could not reach MLB API — {e}")
        if not os.environ.get("CI"):
            input("\nPress Enter to exit...")
        return

    if not games:
        print("  No upcoming games found (off-season or double-check your internet).")
        if not os.environ.get("CI"):
            input("\nPress Enter to exit...")
        return

    print(f"  {len(games)} game(s) found for {game_label}")

    # ---- 3. Get odds ----
    print("\n[3/4] Fetching odds ...")
    raw_odds   = get_mlb_odds(config.ODDS_API_KEY)
    odds_dict  = parse_odds(raw_odds) if raw_odds else {}
    if odds_dict:
        print(f"  Odds for {len(odds_dict)} game(s) received")
    elif not config.ODDS_API_KEY or config.ODDS_API_KEY == "YOUR_API_KEY_HERE":
        print("  No odds — add API key to config.py")
    else:
        print("  No odds returned — lines not posted yet for today's slate (try again later)")

    # ---- 4. Analyze each game ----
    print("\n[4/4] Running model ...\n")

    # Pre-number doubleheaders so every matchup string is unique.
    # Both the console output and downstream keys (Excel, JSON, web) use `matchup`,
    # so we tag here before any processing starts.
    matchup_counts = {}
    for g in games:
        base = f"{g['away_team']}  @  {g['home_team']}"
        matchup_counts[base] = matchup_counts.get(base, 0) + 1
    matchup_seen = {}
    game_matchups = []
    for g in games:
        base = f"{g['away_team']}  @  {g['home_team']}"
        if matchup_counts[base] > 1:
            matchup_seen[base] = matchup_seen.get(base, 0) + 1
            game_matchups.append(f"{base}  (G{matchup_seen[base]})")
        else:
            game_matchups.append(base)

    analyzed = []

    for game, matchup in zip(games, game_matchups):
        away_team    = game["away_team"]
        home_team    = game["home_team"]
        away_pitcher = game["away_pitcher"]
        home_pitcher = game["home_pitcher"]
        venue        = game["venue"]

        print(f"  {matchup}")

        # Pitcher ERA estimates (also capture IP for confidence tier)
        away_era, away_row = find_pitcher_stats(away_pitcher, pitchers_df)
        home_era, home_row = find_pitcher_stats(home_pitcher, pitchers_df)

        # Pitcher not in current-season data (0 IP, IL, hasn't started yet):
        # fall back to prior-year ERA_est so we don't use the flat 4.25 fallback.
        # Pitcher not in current-season data (0 IP, IL, hasn't started yet):
        # fall back to prior-year ERA_est so we don't use the flat 4.25 fallback.
        if away_row is None and prior_pitchers_df is not None and not prior_pitchers_df.empty:
            prior_era, prior_row = find_pitcher_stats(away_pitcher, prior_pitchers_df)
            if prior_row is not None:
                away_era = prior_era
        if home_row is None and prior_pitchers_df is not None and not prior_pitchers_df.empty:
            prior_era, prior_row = find_pitcher_stats(home_pitcher, prior_pitchers_df)
            if prior_row is not None:
                home_era = prior_era

        away_ip       = float(away_row["IP"])  if away_row is not None else 0.0
        home_ip       = float(home_row["IP"])  if home_row is not None else 0.0
        away_actual_era = float(away_row["ERA"]) if away_row is not None else None
        home_actual_era = float(home_row["ERA"]) if home_row is not None else None
        # ERA_est is already blended with player-specific 2025 prior via _rebuild_era_est

        # Apply home/away split — away pitcher is on the road, home pitcher is at home
        # Skip H/A split for pitchers with 0 current-year IP — they're already using
        # the prior-year overall ERA_est, and layering a regression-heavy prior-year
        # H/A split on top can produce artifacts (e.g. Wheeler home 2.19 / road 4.25
        # when his overall prior ERA is 2.99).
        if away_ip > 0:
            away_era = find_pitcher_ha_era(away_pitcher, pitcher_ha_dict,
                                            is_home=False, fallback_era=away_era)
        if home_ip > 0:
            home_era = find_pitcher_ha_era(home_pitcher, pitcher_ha_dict,
                                            is_home=True,  fallback_era=home_era)

        # Recent form blend — 20% of the current-year trust already established.
        # Graduates with _prior_weight: 0 IP → 0%, 12 IP → 6%, 48 IP → 16%, 96 IP → 20%.
        if away_pitcher and away_pitcher in pitcher_recent_form:
            w_recent = _prior_weight(away_ip) * 0.20
            if w_recent > 0:
                away_era = round(away_era * (1 - w_recent) + pitcher_recent_form[away_pitcher] * w_recent, 2)
        if home_pitcher and home_pitcher in pitcher_recent_form:
            w_recent = _prior_weight(home_ip) * 0.20
            if w_recent > 0:
                home_era = round(home_era * (1 - w_recent) + pitcher_recent_form[home_pitcher] * w_recent, 2)

        # Deviation guard: applied LAST so it can't be overwritten by prior/recent blending.
        # Uses percentage thresholds (asymmetric) because ERA scale is non-linear —
        # outperformance approaches 0 so a 15% drop is more meaningful than a 25% rise.
        #
        # Max weight is scaled by IP sample size so tiny-sample actual ERAs (e.g. 11.12
        # in 5.7 IP) don't override the prior-year regression. At 40+ IP the guard runs
        # at full strength (70% max pull); at 5 IP it reaches at most ~9%.
        DEVIATION_OVER_THRESHOLD  = 0.15   # actual is 15%+ below estimate (outperforming)
        DEVIATION_UNDER_THRESHOLD = 0.25   # actual is 25%+ above estimate (underperforming)
        DEVIATION_MAX_WEIGHT      = 0.70
        DEVIATION_FULL_IP         = 40.0   # IP at which guard runs at full strength
        def _deviation_blend(era_est, actual_era, ip_innings):
            if actual_era is None or era_est == 0:
                return era_est
            sample_scale = min(ip_innings / DEVIATION_FULL_IP, 1.0)
            if sample_scale < 0.01:
                return era_est
            pct_diff = (actual_era - era_est) / era_est
            if pct_diff < 0 and abs(pct_diff) > DEVIATION_OVER_THRESHOLD:
                # outperforming — pull toward actual
                excess = abs(pct_diff) - DEVIATION_OVER_THRESHOLD
                w = min(excess / 0.20, 1.0) * DEVIATION_MAX_WEIGHT * sample_scale
            elif pct_diff > 0 and pct_diff > DEVIATION_UNDER_THRESHOLD:
                # underperforming — pull toward actual
                excess = pct_diff - DEVIATION_UNDER_THRESHOLD
                w = min(excess / 0.30, 1.0) * DEVIATION_MAX_WEIGHT * sample_scale
            else:
                return era_est
            return round(era_est * (1 - w) + actual_era * w, 2)

        away_era = _deviation_blend(away_era, away_actual_era, away_ip)
        home_era = _deviation_blend(home_era, home_actual_era, home_ip)

        # Velocity adjustment — +0.20 ERA per mph lost vs prior season, capped at ±0.40.
        # Only fires when delta >= 0.5 mph (below that is day-to-day noise).
        VELO_ERA_PER_MPH = 0.20
        VELO_ADJ_CAP     = 0.40
        for pitcher, is_away in [(away_pitcher, True), (home_pitcher, False)]:
            if not pitcher or pitcher not in velocity_trends:
                continue
            vt    = velocity_trends[pitcher]
            delta = vt["delta"]               # current − prior (mph)
            curr  = vt["current"]
            prior_v = vt["prior"]
            if abs(delta) < 0.5:
                continue
            adj = round(max(-VELO_ADJ_CAP, min(VELO_ADJ_CAP, -delta * VELO_ERA_PER_MPH)), 2)
            if is_away:
                away_era = round(away_era + adj, 2)
            else:
                home_era = round(home_era + adj, 2)
            arrow = "↓" if delta < 0 else "↑"
            side  = "Away" if is_away else "Home"
            print(f"    *** VELO ({side}): {pitcher}  {prior_v} → {curr} mph  ({arrow}{abs(delta):.1f})  ERA adj: {adj:+.2f}")

        away_label       = away_pitcher or "TBD"
        home_label       = home_pitcher or "TBD"
        away_hand        = find_pitcher_hand(away_pitcher, hand_dict)
        home_hand        = find_pitcher_hand(home_pitcher, hand_dict)
        away_platoon_flag = get_platoon_flag(away_pitcher, pitcher_split_dict)
        home_platoon_flag = get_platoon_flag(home_pitcher, pitcher_split_dict)

        # Per-pitcher avg IP/start — needed here for opener detection
        away_avg_ip = ip_per_start_dict.get(away_pitcher)
        home_avg_ip = ip_per_start_dict.get(home_pitcher)

        def _opener_flag(name, avg_ip_per_start):
            if avg_ip_per_start is None:
                return ""
            if avg_ip_per_start < 2.5:
                return f"  *** OPENER LIKELY: {name} averages {avg_ip_per_start:.1f} IP/start — starter contribution will be overestimated"
            if avg_ip_per_start < 3.5:
                return f"  *** SHORT OUTING RISK: {name} averages {avg_ip_per_start:.1f} IP/start"
            return ""

        away_w_pct = round(_prior_weight(away_ip) * 100)
        home_w_pct = round(_prior_weight(home_ip) * 100)
        away_actual_str = f"  actual ERA: {away_actual_era:.2f}" if away_actual_era is not None else ""
        home_actual_str = f"  actual ERA: {home_actual_era:.2f}" if home_actual_era is not None else ""
        print(f"    Away: {away_label:<25}  ERA est: {away_era:.2f}{away_actual_str}  IP: {away_ip:.0f}  "
              f"({away_w_pct}% cur / {100-away_w_pct}% prior)  Hand: {away_hand}")
        if away_platoon_flag:
            print(f"          *** PLATOON ALERT (Away): {away_platoon_flag} — watch for lineup")
        away_opener_str = _opener_flag(away_label, away_avg_ip)
        if away_opener_str:
            print(f"         {away_opener_str}")
        print(f"    Home: {home_label:<25}  ERA est: {home_era:.2f}{home_actual_str}  IP: {home_ip:.0f}  "
              f"({home_w_pct}% cur / {100-home_w_pct}% prior)  Hand: {home_hand}")
        if home_platoon_flag:
            print(f"          *** PLATOON ALERT (Home): {home_platoon_flag} — watch for lineup")
        home_opener_str = _opener_flag(home_label, home_avg_ip)
        if home_opener_str:
            print(f"         {home_opener_str}")

        # Pitcher handedness — determines which team split to use
        away_hand = find_pitcher_hand(away_pitcher, hand_dict)
        home_hand = find_pitcher_hand(home_pitcher, hand_dict)

        # Lineup handedness composition (used for pitcher split ERA + bullpen splits)
        away_lineup_ids  = game.get("away_lineup_ids", [])
        home_lineup_ids  = game.get("home_lineup_ids", [])
        away_hand_pct    = get_lineup_hand_pct(away_lineup_ids, bat_side_dict)
        home_hand_pct    = get_lineup_hand_pct(home_lineup_ids, bat_side_dict)

        lineup_note = ""
        if away_lineup_ids:
            lineup_note += f" away ({len(away_lineup_ids)})"
        if home_lineup_ids:
            lineup_note += f" home ({len(home_lineup_ids)})"
        if lineup_note:
            print(f"    Lineups confirmed:{lineup_note}")

        # Team offensive strength — use platoon split vs pitcher hand, then
        # further adjust for today's confirmed lineup if available
        away_wrc_overall = find_team_wrc_plus(away_team, team_batting_df)
        home_wrc_overall = find_team_wrc_plus(home_team, team_batting_df)

        # Blend current-year wRC+ with prior-year wRC+ based on PA sample size
        away_pa = _get_team_pa(away_team, team_batting_df)
        home_pa = _get_team_pa(home_team, team_batting_df)
        if prior_batting_df is not None and not prior_batting_df.empty:
            away_prior_wrc = find_team_wrc_plus(away_team, prior_batting_df)
            home_prior_wrc = find_team_wrc_plus(home_team, prior_batting_df)
            away_wrc_overall = round(
                _team_prior_weight(away_pa) * away_wrc_overall +
                (1 - _team_prior_weight(away_pa)) * away_prior_wrc
            )
            home_wrc_overall = round(
                _team_prior_weight(home_pa) * home_wrc_overall +
                (1 - _team_prior_weight(home_pa)) * home_prior_wrc
            )

        away_wrc_base = find_team_split_wrc(
            away_team, split_dict, home_hand, away_wrc_overall
        ) if home_pitcher and home_pitcher != "TBD" else away_wrc_overall

        home_wrc_base = find_team_split_wrc(
            home_team, split_dict, away_hand, home_wrc_overall
        ) if away_pitcher and away_pitcher != "TBD" else home_wrc_overall

        # Recent form blend — 20% of the current-year trust already established.
        # Graduates with _team_prior_weight: low PA → near 0%, full season → 20%.
        if away_team in team_recent_form:
            w_recent = _team_prior_weight(away_pa) * 0.20
            if w_recent > 0:
                away_wrc_base = round(away_wrc_base * (1 - w_recent) + team_recent_form[away_team] * w_recent)
        if home_team in team_recent_form:
            w_recent = _team_prior_weight(home_pa) * 0.20
            if w_recent > 0:
                home_wrc_base = round(home_wrc_base * (1 - w_recent) + team_recent_form[home_team] * w_recent)

        away_wrc = adjust_wrc_for_lineup(away_lineup_ids, away_wrc_base, batter_ops)
        home_wrc = adjust_wrc_for_lineup(home_lineup_ids, home_wrc_base, batter_ops)

        # Build split-step label so we can see where each drop came from
        away_breakdown = f"{away_team.split()[-1]}: {away_wrc_overall}→{away_wrc_base}(split)→{away_wrc}(lineup)"
        home_breakdown = f"{home_team.split()[-1]}: {home_wrc_overall}→{home_wrc_base}(split)→{home_wrc}(lineup)"
        print(f"    wRC+: {away_team.split()[-1]} {away_wrc}  |  {home_team.split()[-1]} {home_wrc}  "
              f"(overall: {away_wrc_overall} / {home_wrc_overall})")
        print(f"    wRC+ breakdown: {away_breakdown} | {home_breakdown}")

        # Defensive adjustment — unearned runs allowed per game, blended with prior year
        # A poor defensive team gives opponents extra expected runs
        LG_AVG_UNEARNED = 0.35
        def _get_defense(team_name, cur_dict, pri_dict):
            cur = cur_dict.get(team_name)
            pri = pri_dict.get(team_name)
            lg  = cur_dict.get("__league_avg__", LG_AVG_UNEARNED)
            if cur is None and pri is None:
                return LG_AVG_UNEARNED
            if cur is None:
                return pri
            if pri is None:
                return cur
            # Blend using same game-count proxy (1 game ≈ 9 team PA ≈ 0.45 IP-equiv)
            games_played = sum(1 for v in cur_dict.values()
                               if isinstance(v, float) and v != lg)
            w = min(games_played / 81, 1.0)   # 50% weight at half season
            return round(w * cur + (1 - w) * pri, 4)

        away_defense = _get_defense(away_team, defense_dict, prior_defense_dict)
        home_defense = _get_defense(home_team, defense_dict, prior_defense_dict)

        # Pitcher ERA — adjusted for opposing lineup handedness composition
        # (e.g. a RHP faces a left-heavy lineup → his ERA vs LHB gets more weight)
        away_era = find_pitcher_split_era(away_pitcher, pitcher_split_dict,
                                           home_hand_pct, away_era)
        home_era = find_pitcher_split_era(home_pitcher, pitcher_split_dict,
                                           away_hand_pct, home_era)

        # Bullpen ERA — platoon-adjusted, then degraded for yesterday's usage
        away_bullpen = find_team_bullpen_era(away_team, bullpen_dict, home_hand_pct)
        home_bullpen = find_team_bullpen_era(home_team, bullpen_dict, away_hand_pct)

        away_bp_usage  = bullpen_usage.get(away_team, {})
        home_bp_usage  = bullpen_usage.get(home_team, {})
        away_bp_status = away_bp_usage.get("status", "fresh")
        home_bp_status = home_bp_usage.get("status", "fresh")
        away_bp_decay  = away_bp_usage.get("decay", 1.0)
        home_bp_decay  = home_bp_usage.get("decay", 1.0)
        away_unavail   = away_bp_usage.get("unavailable", [])
        home_unavail   = home_bp_usage.get("unavailable", [])

        # For taxed teams: quality-aware adjustment (losing a closer hurts more than
        # losing a mop-up arm; individual arm ERA vs team baseline drives the multiplier)
        if away_bp_status == "taxed":
            away_bp_adj = _quality_bp_adj(away_unavail, away_bullpen, pitchers_df, away_bp_decay)
        else:
            away_bp_adj = away_bp_usage.get("era_adjustment", 0.0)

        if home_bp_status == "taxed":
            home_bp_adj = _quality_bp_adj(home_unavail, home_bullpen, pitchers_df, home_bp_decay)
        else:
            home_bp_adj = home_bp_usage.get("era_adjustment", 0.0)

        if away_bp_adj != 0:
            away_bullpen = round(away_bullpen + away_bp_adj, 2)
            away_quest   = away_bp_usage.get("questionable", [])
            if away_bp_adj < 0:
                print(f"    *** BULLPEN FRESH (Away): ERA {away_bp_adj:.2f}  (well-rested)")
            else:
                unavail_str = ", ".join(f"{n} ({p}p)" for n, p in away_unavail)
                quest_str   = ", ".join(f"{n} ({p}p)" for n, p in away_quest)
                parts = []
                if unavail_str: parts.append(f"unavail: {unavail_str}")
                if quest_str:   parts.append(f"quest: {quest_str}")
                print(f"    *** BULLPEN {away_bp_status.upper()} (Away): ERA +{away_bp_adj:.2f} adj"
                      f"  [{'; '.join(parts)}]")

        if home_bp_adj != 0:
            home_bullpen = round(home_bullpen + home_bp_adj, 2)
            home_quest   = home_bp_usage.get("questionable", [])
            if home_bp_adj < 0:
                print(f"    *** BULLPEN FRESH (Home): ERA {home_bp_adj:.2f}  (well-rested)")
            else:
                unavail_str = ", ".join(f"{n} ({p}p)" for n, p in home_unavail)
                quest_str   = ", ".join(f"{n} ({p}p)" for n, p in home_quest)
                parts = []
                if unavail_str: parts.append(f"unavail: {unavail_str}")
                if quest_str:   parts.append(f"quest: {quest_str}")
                print(f"    *** BULLPEN {home_bp_status.upper()} (Home): ERA +{home_bp_adj:.2f} adj"
                      f"  [{'; '.join(parts)}]")

        # Rest days
        away_rest = rest_dict.get(away_team, 2)
        home_rest = rest_dict.get(home_team, 2)
        if away_rest <= 1 or home_rest <= 1:
            back_to_back = []
            if away_rest <= 1: back_to_back.append(away_team.split()[-1])
            if home_rest <= 1: back_to_back.append(home_team.split()[-1])
            print(f"    Back-to-back: {', '.join(back_to_back)}")

        # Alternate venue override (e.g. Mexico City international series)
        from model import ALTERNATE_VENUE_FACTORS
        alt_park = ALTERNATE_VENUE_FACTORS.get(venue.lower().strip()) if venue else None
        if alt_park is not None:
            print(f"    *** NEUTRAL SITE: {venue}  →  park factor {alt_park:.2f}  (overrides {home_team.split()[-1]} home factor)")

        # Weather
        weather = get_weather(venue, game.get("game_time"))
        weather_factor = weather["run_factor"] if weather else 1.0
        if weather and not weather.get("dome"):
            print(f"    Weather: {weather['description']}  →  run factor {weather_factor:.3f}")

        # Umpire run factor
        hp_umpire     = get_game_umpire(game.get("officials", []))
        umpire_factor = get_umpire_run_factor(hp_umpire, umpire_tendencies)
        if hp_umpire:
            direction = ("offense-friendly" if umpire_factor > 1.02
                         else "pitcher-friendly" if umpire_factor < 0.98
                         else "neutral")
            print(f"    Umpire: {hp_umpire}  →  run factor {umpire_factor:.3f}  ({direction})")

        # Combined run environment (weather × umpire)
        env_factor = round(weather_factor * umpire_factor, 4)

        # Defensive adjustment: poor defense → opponents score more unearned runs
        # Applied as a small additive boost to each team's expected runs via
        # a multiplicative factor relative to league average
        LG_UE = defense_dict.get("__league_avg__", LG_AVG_UNEARNED)
        away_def_factor = round(1.0 + (home_defense - LG_UE) / 4.5, 4)  # home pitches to away batters
        home_def_factor = round(1.0 + (away_defense - LG_UE) / 4.5, 4)  # away pitches to home batters
        print(f"    Defense: {away_team.split()[-1]} {away_defense:.3f} UE/g (factor {home_def_factor:.3f})"
              f"  |  {home_team.split()[-1]} {home_defense:.3f} UE/g (factor {away_def_factor:.3f})"
              f"  |  LG avg {LG_UE:.3f}")

        # Win probabilities
        probs = calculate_game_probability(
            home_wrc_plus        = home_wrc,
            away_wrc_plus        = away_wrc,
            home_pitcher_era_est = home_era,
            away_pitcher_era_est = away_era,
            venue                = venue,
            home_bullpen_era     = home_bullpen,
            away_bullpen_era     = away_bullpen,
            weather_factor       = env_factor,
            home_rest_days       = home_rest,
            away_rest_days       = away_rest,
            away_defense_factor  = away_def_factor,
            home_defense_factor  = home_def_factor,
            home_pitcher_avg_ip  = home_avg_ip,
            away_pitcher_avg_ip  = away_avg_ip,
            home_team            = home_team,
            away_team            = away_team,
        )

        home_prob  = probs["home_win_prob"]
        away_prob  = probs["away_win_prob"]
        home_exp_r = probs["home_exp_runs"]
        away_exp_r = probs["away_exp_runs"]

        # ---- Match odds ----
        game_odds = match_odds_game(away_team, home_team, odds_dict, game.get("game_time"))

        bets = []

        if game_odds:
            ml_away = game_odds["moneyline"]["away"]
            ml_home = game_odds["moneyline"]["home"]
            rl_away = game_odds["runline"]["away"]
            rl_home = game_odds["runline"]["home"]
            tot_ovr = game_odds["total"]["over"]
            tot_und = game_odds["total"]["under"]
            tot_line = game_odds["total"]["line"]

            if tot_line is not None:
                print(f"    Book total: {tot_line}")

            # -- Moneylines --
            for prob, team, odds in [(away_prob, away_team, ml_away),
                                      (home_prob, home_team, ml_home)]:
                b = analyze_bet(prob, odds, config.BANKROLL,
                                config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                if b:
                    bets.append({**b,
                                  "market": "Moneyline",
                                  "team": team,
                                  "bet_type_label": "ML"})

            # -- Run Lines --
            home_minus_prob, away_plus_prob, away_minus_prob, home_plus_prob = \
                calculate_runline_probabilities(home_prob, home_exp_r, away_exp_r)

            # Determine actual spread direction from the API point values.
            # Away team is the -1.5 favorite when their spread point is negative.
            away_pt = game_odds["runline"].get("away_point")
            away_is_fav = (away_pt is not None and away_pt < 0)

            if away_is_fav:
                rl_pairs = [
                    (away_minus_prob, away_team, rl_away, "-1.5"),
                    (home_plus_prob,  home_team, rl_home, "+1.5"),
                ]
            else:
                rl_pairs = [
                    (home_minus_prob, home_team, rl_home, "-1.5"),
                    (away_plus_prob,  away_team, rl_away, "+1.5"),
                ]
            for prob, team, odds, label in rl_pairs:
                b = analyze_bet(prob, odds, config.BANKROLL,
                                config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                if b:
                    bets.append({**b,
                                  "market": "Run Line",
                                  "team": team,
                                  "bet_type_label": label})

            # -- Totals --
            if tot_line is not None:
                over_prob, under_prob = calculate_over_probability(
                    home_exp_r, away_exp_r, tot_line
                )
                for prob, side, odds in [
                    (over_prob,  f"Over {tot_line}",  tot_ovr),
                    (under_prob, f"Under {tot_line}", tot_und)
                ]:
                    b = analyze_bet(prob, odds, config.BANKROLL,
                                    config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                    if b:
                        bets.append({**b,
                                      "market": "Total",
                                      "team": side,
                                      "bet_type_label": ""})

            # ---- First 5 Innings ----
            # F5 uses starter ERA only (no bullpen blending) — our strongest signal
            if away_label != "TBD" and home_label != "TBD":
                f5 = calculate_f5_probability(
                    home_wrc_plus        = home_wrc,
                    away_wrc_plus        = away_wrc,
                    home_pitcher_era_est = home_era,
                    away_pitcher_era_est = away_era,
                    venue                = venue,
                    weather_factor       = env_factor,
                    away_defense_factor  = away_def_factor,
                    home_defense_factor  = home_def_factor,
                    home_team            = home_team,
                    away_starter_ip      = _f5_starter_ip(away_pitcher, pitcher_recent_starts),
                    home_starter_ip      = _f5_starter_ip(home_pitcher, pitcher_recent_starts),
                    away_bullpen_era     = away_bullpen,
                    home_bullpen_era     = home_bullpen,
                )
                f5_home_prob  = f5["home_win_prob"]
                f5_away_prob  = f5["away_win_prob"]
                f5_home_exp_r = f5["home_exp_runs"]
                f5_away_exp_r = f5["away_exp_runs"]

                f5_ml_away = game_odds["f5_moneyline"]["away"]
                f5_ml_home = game_odds["f5_moneyline"]["home"]
                f5_rl_away = game_odds["f5_runline"]["away"]
                f5_rl_home = game_odds["f5_runline"]["home"]
                f5_tot_ovr = game_odds["f5_total"]["over"]
                f5_tot_und = game_odds["f5_total"]["under"]
                f5_tot_line = game_odds["f5_total"]["line"]

                # F5 Moneylines
                for prob, team, odds in [(f5_away_prob, away_team, f5_ml_away),
                                          (f5_home_prob, home_team, f5_ml_home)]:
                    b = analyze_bet(prob, odds, config.BANKROLL,
                                    config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                    if b:
                        bets.append({**b,
                                      "market": "F5 Moneyline",
                                      "team": team,
                                      "bet_type_label": "F5 ML"})

                # F5 Run Lines
                f5_home_minus, f5_away_plus, f5_away_minus, f5_home_plus = \
                    calculate_runline_probabilities(f5_home_prob, f5_home_exp_r, f5_away_exp_r)

                if away_is_fav:
                    f5_rl_pairs = [
                        (f5_away_minus, away_team, f5_rl_away, "F5 -0.5"),
                        (f5_home_plus,  home_team, f5_rl_home, "F5 +0.5"),
                    ]
                else:
                    f5_rl_pairs = [
                        (f5_home_minus, home_team, f5_rl_home, "F5 -0.5"),
                        (f5_away_plus,  away_team, f5_rl_away, "F5 +0.5"),
                    ]
                for prob, team, odds, label in f5_rl_pairs:
                    b = analyze_bet(prob, odds, config.BANKROLL,
                                    config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                    if b:
                        bets.append({**b,
                                      "market": "F5 Run Line",
                                      "team": team,
                                      "bet_type_label": label})

                # F5 Totals
                if f5_tot_line is not None:
                    f5_over_prob, f5_under_prob = calculate_over_probability(
                        f5_home_exp_r, f5_away_exp_r, f5_tot_line
                    )
                    for prob, side, odds in [
                        (f5_over_prob,  f"F5 Over {f5_tot_line}",  f5_tot_ovr),
                        (f5_under_prob, f"F5 Under {f5_tot_line}", f5_tot_und)
                    ]:
                        b = analyze_bet(prob, odds, config.BANKROLL,
                                        config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                        if b:
                            bets.append({**b,
                                          "market": "F5 Total",
                                          "team": side,
                                          "bet_type_label": ""})

            # ---- Strikeout Props ----
            k_props = game_odds.get("k_props", {})
            for pitcher_name, pitcher_team, opp_team in [
                (away_label, away_team, home_team),
                (home_label, home_team, away_team)
            ]:
                if pitcher_name == "TBD" or pitcher_name not in k_props:
                    continue

                kp = k_props[pitcher_name]
                if kp.get("line") is None:
                    continue

                _, p_row = find_pitcher_stats(pitcher_name, pitchers_df)
                if p_row is None:
                    continue

                try:
                    pitcher_k_pct = float(p_row["K%"])
                except (TypeError, ValueError, KeyError):
                    continue

                opp_k_rate = find_team_k_rate(opp_team, team_batting_df)
                exp_k      = calculate_strikeout_projection(pitcher_k_pct, opp_k_rate)
                if exp_k is None:
                    continue

                k_line = float(kp["line"])
                # Simple probability: use normal approximation around expected K
                # Variance for K count ~ exp_k * (1 - pitcher_k_pct) (binomial)
                import math
                std_k = math.sqrt(max(exp_k * (1 - pitcher_k_pct), 0.5))
                from scipy.stats import norm
                over_prob_k  = round(1 - norm.cdf(k_line, loc=exp_k, scale=std_k), 4)
                under_prob_k = round(norm.cdf(k_line, loc=exp_k, scale=std_k), 4)

                for prob, side, odds, label in [
                    (over_prob_k,  f"{pitcher_name} Over {k_line}K",  kp.get("over"),  f"K O{k_line}"),
                    (under_prob_k, f"{pitcher_name} Under {k_line}K", kp.get("under"), f"K U{k_line}")
                ]:
                    b = analyze_bet(prob, odds, config.BANKROLL,
                                    config.KELLY_FRACTION, config.MIN_EV_THRESHOLD)
                    if b:
                        bets.append({**b,
                                      "market":         "K Prop",
                                      "team":           side,
                                      "bet_type_label": label})

        else:
            ml_away = ml_home = None
            tot_line = None

        # Deduplicate: keep only highest-EV bet per team (avoid ML + runline overlap)
        best_by_team = {}
        for bet in bets:
            team = bet["team"]
            if team not in best_by_team or bet["ev"] > best_by_team[team]["ev"]:
                best_by_team[team] = bet
        bets = list(best_by_team.values())

        # Cross-team hedge dedup: ML on one team + opponent +1.5 partially cancel each other
        # (they win in opposite scenarios). Keep only the higher-EV of the two.
        ml_bets      = {b["team"]: b for b in bets if b["market"] in ("Moneyline", "F5 Moneyline")}
        rl_plus_bets = {b["team"]: b for b in bets
                        if b["market"] in ("Run Line", "F5 Run Line")
                        and "+1.5" in b.get("bet_type_label", "")}
        for ml_team, ml_bet in list(ml_bets.items()):
            opp = home_team if ml_team == away_team else away_team
            if opp in rl_plus_bets:
                rl_bet = rl_plus_bets[opp]
                if ml_bet["ev"] >= rl_bet["ev"]:
                    bets = [b for b in bets if b is not rl_bet]
                    print(f"    Dedup (hedge): dropped {opp} +1.5 ({rl_bet['ev_pct']}) — keeping {ml_team} ML ({ml_bet['ev_pct']})")
                else:
                    bets = [b for b in bets if b is not ml_bet]
                    print(f"    Dedup (hedge): dropped {ml_team} ML ({ml_bet['ev_pct']}) — keeping {opp} +1.5 ({rl_bet['ev_pct']})")

        # Mark priority and fade bets (backtest-optimized filters)
        for b in bets:
            b["priority"] = _is_priority_bet(b)
            b["fade"]     = _is_fade_bet(b, tot_line)

        # Print any flagged bets
        if bets:
            priority_bets = [b for b in bets if b["priority"]]
            fade_bets     = [b for b in bets if b["fade"]]
            header = f"    >>> {len(bets)} BET(S) FLAGGED"
            if priority_bets:
                header += f"  |  {len(priority_bets)} PRIORITY ★"
            if fade_bets:
                header += f"  |  {len(fade_bets)} FADE WATCH ⚠"
            print(header + ":")
            for bet in bets:
                bet_desc = f"{bet['team']}  {bet['bet_type_label']}".rstrip()
                # Market disagreement check: warn when model and book implied prob
                # diverge by 30+ percentage points — likely a bad input driving the edge
                from ev_calculator import american_to_decimal
                book_imp = round(1.0 / american_to_decimal(bet["book_odds"]), 3)
                disagreement = abs(bet["model_prob"] - book_imp)
                flag = "  *** SANITY CHECK: model vs market gap >{:.0f}pp — verify inputs".format(
                    disagreement * 100) if disagreement >= 0.30 else ""
                priority_tag = "  ★ PRIORITY" if bet["priority"] else ""
                fade_tag     = "  ⚠ FADE WATCH" if bet["fade"] else ""
                print(f"        {bet_desc}"
                      f"  @  {bet['book_odds']:+d}"
                      f"  |  EV: {bet['ev_pct']}"
                      f"  |  Bet: ${bet['bet_amount']:.2f}"
                      f"{flag}{priority_tag}{fade_tag}")
        else:
            print(f"    No +EV bets found (home prob: {home_prob:.1%})")
        print()

        # Data quality tier — measures ERA estimate reliability based on starter IP.
        # Higher IP = more current-season data, less reliance on prior-year regression.
        # NOTE: this is NOT a signal of bet quality. Early-season (Low/Medium) bets
        # that rely on prior-year stats can outperform High-data bets when current-year
        # samples are too small to be trusted over stable prior-year baselines.
        #   Low    < 20 IP  (~1-2 starts): 90%+ prior-year weight
        #   Medium  20-40 IP (~3-5 starts): mixed current/prior
        #   High   40+ IP  (~6+ starts):  mostly current-season data
        min_ip = min(away_ip, home_ip)
        if away_label == "TBD" or home_label == "TBD":
            confidence = "Low"
        elif min_ip >= 40:
            confidence = "High"
        elif min_ip >= 20:
            confidence = "Medium"
        else:
            confidence = "Low"

        analyzed.append({
            "matchup":            matchup,
            "game_time_et":       _et_time(game["game_time"]),
            "venue":              venue,
            "away_team":          away_team,
            "home_team":          home_team,
            "away_pitcher":       away_label,
            "home_pitcher":       home_label,
            "away_era_est":       round(away_era, 2),
            "home_era_est":       round(home_era, 2),
            "away_actual_era":    away_actual_era,
            "home_actual_era":    home_actual_era,
            "away_ip":            away_ip,
            "home_ip":            home_ip,
            "away_wrc_plus":      away_wrc,
            "home_wrc_plus":      home_wrc,
            "away_bullpen_era":   round(away_bullpen, 2),
            "home_bullpen_era":   round(home_bullpen, 2),
            "away_exp_runs":      away_exp_r,
            "home_exp_runs":      home_exp_r,
            "away_win_prob":      away_prob,
            "home_win_prob":      home_prob,
            "home_model_odds":    prob_to_american_odds(home_prob),
            "away_model_odds":    prob_to_american_odds(away_prob),
            "home_ml_odds":       ml_home,
            "away_ml_odds":       ml_away,
            "park_factor":        probs["park_factor"],
            "weather":             weather["description"] if weather else "N/A",
            "weather_factor":      weather_factor,
            "umpire":              hp_umpire or "TBD",
            "umpire_factor":       umpire_factor,
            "lineup_confirmed":    bool(away_lineup_ids or home_lineup_ids),
            "away_lineup_count":   len(away_lineup_ids),
            "home_lineup_count":   len(home_lineup_ids),
            "away_platoon_flag":   away_platoon_flag,
            "home_platoon_flag":   home_platoon_flag,
            "away_avg_ip_per_start":  away_avg_ip,
            "home_avg_ip_per_start":  home_avg_ip,
            "away_bp_era_adj":        away_bp_adj,
            "home_bp_era_adj":        home_bp_adj,
            "away_bp_status":         away_bp_usage.get("status", "fresh"),
            "home_bp_status":         home_bp_usage.get("status", "fresh"),
            "proj_score":          f"{away_exp_r:.1f}  –  {home_exp_r:.1f}",
            "book_total_line":     tot_line,
            "confidence":          confidence,
            "bets":                bets
        })

    # ---- Save picks to JSON (for results checker + multi-run merging) ----
    import json

    def _game_to_json(g, game_pk):
        return {
            "game_pk":           game_pk,
            "game_time_et":      g["game_time_et"],
            "matchup":           g["matchup"],
            "away_team":         g["away_team"],
            "home_team":         g["home_team"],
            "away_pitcher":      g["away_pitcher"],
            "home_pitcher":      g["home_pitcher"],
            "venue":             g.get("venue", ""),
            "proj_score":        g["proj_score"],
            "confidence":        g["confidence"],
            "away_era_est":      g.get("away_era_est"),
            "home_era_est":      g.get("home_era_est"),
            "away_actual_era":   g.get("away_actual_era"),
            "home_actual_era":   g.get("home_actual_era"),
            "away_ip":           g.get("away_ip", 0),
            "home_ip":           g.get("home_ip", 0),
            "away_wrc_plus":     g.get("away_wrc_plus", 100),
            "home_wrc_plus":     g.get("home_wrc_plus", 100),
            "away_bullpen_era":  g.get("away_bullpen_era"),
            "home_bullpen_era":  g.get("home_bullpen_era"),
            "away_exp_runs":     g.get("away_exp_runs"),
            "home_exp_runs":     g.get("home_exp_runs"),
            "away_win_prob":     g.get("away_win_prob"),
            "home_win_prob":     g.get("home_win_prob"),
            "home_model_odds":   g.get("home_model_odds"),
            "away_model_odds":   g.get("away_model_odds"),
            "home_ml_odds":      g.get("home_ml_odds"),
            "away_ml_odds":      g.get("away_ml_odds"),
            "park_factor":       g.get("park_factor", 1.0),
            "weather":           g.get("weather", "N/A"),
            "umpire":            g.get("umpire", "TBD"),
            "umpire_factor":     g.get("umpire_factor", 1.0),
            "lineup_confirmed":   g.get("lineup_confirmed", False),
            "away_lineup_count":  g.get("away_lineup_count", 0),
            "home_lineup_count":  g.get("home_lineup_count", 0),
            "away_platoon_flag":       g.get("away_platoon_flag"),
            "home_platoon_flag":       g.get("home_platoon_flag"),
            "away_avg_ip_per_start":   g.get("away_avg_ip_per_start"),
            "home_avg_ip_per_start":   g.get("home_avg_ip_per_start"),
            "away_bp_era_adj":         g.get("away_bp_era_adj", 0.0),
            "home_bp_era_adj":         g.get("home_bp_era_adj", 0.0),
            "away_bp_status":          g.get("away_bp_status", "fresh"),
            "home_bp_status":          g.get("home_bp_status", "fresh"),
            "book_total_line":         g.get("book_total_line"),
            "bets": [
                {
                    "market":         b["market"],
                    "team":           b["team"],
                    "bet_type_label": b["bet_type_label"],
                    "book_odds":      b["book_odds"],
                    "model_odds":     b["model_odds"],
                    "bet_amount":     b["bet_amount"],
                    "model_prob":     b["model_prob"],
                    "ev":             b["ev"],
                    "ev_pct":         b["ev_pct"],
                    "total_line":     (
                        float(b["team"].split()[-1])
                        if b["market"] == "Total" else None
                    ),
                    "priority":       b.get("priority", False),
                    "fade":           b.get("fade", False),
                }
                for b in g["bets"]
            ]
        }

    picks_data = {
        "date":  games_date.strftime("%Y-%m-%d"),
        "games": [
            _game_to_json(g, games[i]["game_pk"])
            for i, g in enumerate(analyzed)
        ]
    }

    picks_file = os.path.join("data", f"picks_{picks_data['date']}.json")

    # Merge: keep prior games not in this run, add/update current run's games
    if os.path.exists(picks_file):
        with open(picks_file) as f:
            existing = json.load(f)
        new_pks = {g["game_pk"] for g in picks_data["games"]}
        carried  = [g for g in existing.get("games", []) if g["game_pk"] not in new_pks]
        for g in carried:
            if "matchup" not in g:
                g["matchup"] = f"{g['away_team']}  @  {g['home_team']}"
        print(f"  Merging: {len(carried)} carried from earlier run + {len(picks_data['games'])} from this run")
        picks_data["games"] = carried + picks_data["games"]
        picks_data["games"].sort(key=lambda g: g["game_time_et"])
    else:
        print(f"  No prior picks file found — starting fresh ({len(picks_data['games'])} games)")

    with open(picks_file, "w") as f:
        json.dump(picks_data, f, indent=2)
    print(f"Picks saved to {picks_file}")

    # Push picks to web (traviswhawthorne.net/mlb)
    try:
        from push_picks_to_web import push_picks
        push_picks()
    except Exception as e:
        print(f"  Web push skipped: {e}")

    # ---- Write Excel from full merged day ----
    print("Writing picks to Excel ...")
    out = write_picks_to_excel(picks_data["games"], config.OUTPUT_FILE, games_date)

    total_bets   = sum(len(g["bets"]) for g in analyzed)
    total_stake  = sum(b["bet_amount"] for g in analyzed for b in g["bets"])

    print()
    print("=" * 62)
    print(f"  DONE — {total_bets} bet(s) recommended today")
    if total_bets > 0:
        print(f"  Total stake:  ${total_stake:.2f}")
    print(f"  File:  {os.path.abspath(out)}")
    print("=" * 62)
    print()

    print(f"\nLog saved: {_log_path}")
    if not os.environ.get("CI"):
        input("Press Enter to close...")
    _log_file.close()


# ------------------------------------------------------------------ #
if __name__ == "__main__":
    main()
