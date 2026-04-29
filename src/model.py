"""
MLB Win Probability Model
=========================
Uses pitcher xFIP/SIERA and team wRC+ to estimate win probabilities.

Core logic:
  1. Estimate how many runs each team is expected to score
       - Based on their offensive strength (wRC+)
       - And how good the opposing pitcher is (xFIP/SIERA blend)
       - Adjusted for ballpark run environment
  2. Model each team's run total as a Poisson distribution
  3. Calculate P(home wins) by summing all combinations where home > away
  4. Apply a home field advantage adjustment
"""

import numpy as np
from scipy.stats import nbinom

# Negative binomial dispersion parameter for MLB run scoring.
# Fitted via MLE from 4,856 team-games (2025 full season).
# Poisson assumes variance = mean (~4.4). Actual variance ~10.5 → r = 3.14.
# NB fit: 0-run games 6.26% (actual 6.82%) vs Poisson 1.17%. Far more accurate.
RUN_DISPERSION = 3.14

def _nb_pmf(k, mu):
    """Negative binomial PMF: runs ~ NB(r=RUN_DISPERSION, mean=mu)."""
    p = RUN_DISPERSION / (RUN_DISPERSION + mu)
    return nbinom.pmf(k, n=RUN_DISPERSION, p=p)

# ---- League averages (update each season) ----
LEAGUE_AVG_RUNS    = 4.43   # Data: 9,734 games 2022-2025 (4.434 runs/team/game)
LEAGUE_AVG_XFIP    = 4.20   # Average starter ERA estimator
LEAGUE_AVG_WRC_PLUS = 100   # By definition, always 100

# Home field advantage: home teams win ~54% of MLB games historically.
# Raw run scoring difference is only +0.4% (data: 4.442 vs 4.425 runs/game).
# We use 1.03 rather than 1.004 because our Poisson model doesn't capture the
# batting-last strategic advantage — inflating runs slightly produces the correct
# ~54% home win rate from the model.
HOME_FIELD_RUN_BOOST = 1.03

# Systematic run under-projection correction.
# Backtests show the model consistently under-projects game totals:
#   2024 (Jul-Sep): -0.13 runs/game combined → -0.065 per team
#   2025 (Jul-Sep): -0.40 runs/game combined → -0.20  per team
# Average bias: -0.265 combined → +0.13 per team added here.
# Over rate without offset: ~53.3% both years. Target: ~50%.
RUN_CALIBRATION_OFFSET = 0.13

# Bullpen blending: baseline starter innings at league-average ERA.
# Actual innings adjust dynamically — bad starters get pulled earlier,
# giving elite bullpens more exposure against the opposing lineup.
STARTER_INNINGS  = 5.26  # baseline at league-avg ERA (13,811 starts 2023-2025)
BULLPEN_INNINGS  = 3.74  # 9 - STARTER_INNINGS

# How much each run above/below league ERA shifts starter innings.
# Data-backed from 13,811 starts (2023-2025): slope = -0.201 IP per ERA run.
STARTER_IP_PER_ERA_RUN = 0.20

# ---- Ballpark run factors (FanGraphs 5-year regressed, keyed by home team) ----
# Source: FanGraphs Guts page — Basic (5yr) column, 2024 season
# Methodology: compares each team's home run environment vs their road games,
#   using the same team home vs road (controls for team quality / home pitching).
#   Multi-year regression reduces small-sample noise.
# Values centered at 1.00 (1.05 = 5% more runs, 0.94 = 6% fewer runs)
# Applied to away team offense only — home team effect is embedded in TEAM_HOME_FACTOR.
PARK_FACTORS = {
    "Colorado Rockies":           1.13,
    "Cincinnati Reds":            1.05,
    "Boston Red Sox":             1.04,
    "Athletics":                  1.03,
    "Kansas City Royals":         1.03,
    "Pittsburgh Pirates":         1.02,
    "Los Angeles Angels":         1.01,
    "Minnesota Twins":            1.01,
    "Miami Marlins":              1.01,
    "Tampa Bay Rays":             1.01,
    "Arizona Diamondbacks":       1.01,
    "Philadelphia Phillies":      1.01,
    "Atlanta Braves":             1.00,
    "Detroit Tigers":             1.00,
    "Chicago White Sox":          1.00,
    "Washington Nationals":       1.00,
    "Houston Astros":             0.99,
    "Baltimore Orioles":          0.99,
    "New York Yankees":           0.99,
    "Texas Rangers":              0.99,
    "Toronto Blue Jays":          0.99,
    "Los Angeles Dodgers":        0.99,
    "Milwaukee Brewers":          0.99,
    "St. Louis Cardinals":        0.98,
    "Chicago Cubs":               0.98,
    "San Francisco Giants":       0.97,
    "New York Mets":              0.96,
    "San Diego Padres":           0.96,
    "Cleveland Guardians":        0.99,
    "Seattle Mariners":           0.94,
}

# ---- Alternate / neutral venue overrides ----
# Games played at non-home stadiums (international series, neutral sites).
# These override the home team's park factor when the venue name matches.
# Mexico City: Estadio Alfredo Harp Helú — altitude ~7,350 ft (vs Coors ~5,280 ft).
# Air density ~20% lower than sea level vs ~13% at Coors. Estimated run factor 1.20.
ALTERNATE_VENUE_FACTORS = {
    "estadio alfredo harp helu": 1.20,
    "estadio alfredo harp helú": 1.20,
}


# ---- Team home/away run factors ----
# Source: park_factors_analysis.py — run to regenerate from MLB Stats API data
# home_factor = team's home rpg / their overall rpg  (park effect embedded — do NOT
#               apply park_factor separately for the home team)
# away_factor = team's away rpg / their overall rpg  (road tendency; park_factor
#               for the specific venue is applied separately to away team offense)
# Fallback for unknown teams: HOME_FIELD_RUN_BOOST / 1.0
TEAM_HOME_FACTOR = {
    "Colorado Rockies":               1.220,
    "Philadelphia Phillies":          1.067,
    "Kansas City Royals":             1.046,
    "Pittsburgh Pirates":             1.034,
    "Cincinnati Reds":                1.027,
    "Boston Red Sox":                 1.024,
    "Minnesota Twins":                1.021,
    "Los Angeles Angels":             1.019,
    "St. Louis Cardinals":            1.016,
    "Arizona Diamondbacks":           1.016,
    "Detroit Tigers":                 1.009,
    "Miami Marlins":                  1.009,
    "Texas Rangers":                  1.008,
    "Tampa Bay Rays":                 1.007,
    "Athletics":                      1.004,
    "New York Mets":                  1.000,
    "Los Angeles Dodgers":            0.995,
    "New York Yankees":               0.983,
    "Baltimore Orioles":              0.981,
    "Toronto Blue Jays":              0.981,
    "Milwaukee Brewers":              0.980,
    "Washington Nationals":           0.979,
    "Chicago White Sox":              0.978,
    "Chicago Cubs":                   0.976,
    "Cleveland Guardians":            0.974,
    "Atlanta Braves":                 0.967,
    "San Francisco Giants":           0.966,
    "Houston Astros":                 0.964,
    "San Diego Padres":               0.950,
    "Oakland Athletics":              0.940,
    "Seattle Mariners":               0.922,
}

TEAM_AWAY_FACTOR = {
    "Colorado Rockies":               0.780,
    "Philadelphia Phillies":          0.933,
    "Kansas City Royals":             0.954,
    "Pittsburgh Pirates":             0.966,
    "Cincinnati Reds":                0.973,
    "Boston Red Sox":                 0.976,
    "Minnesota Twins":                0.979,
    "Los Angeles Angels":             0.981,
    "St. Louis Cardinals":            0.983,
    "Arizona Diamondbacks":           0.984,
    "Detroit Tigers":                 0.991,
    "Miami Marlins":                  0.991,
    "Texas Rangers":                  0.992,
    "Tampa Bay Rays":                 0.993,
    "Athletics":                      0.996,
    "New York Mets":                  1.000,
    "Los Angeles Dodgers":            1.005,
    "New York Yankees":               1.017,
    "Baltimore Orioles":              1.019,
    "Toronto Blue Jays":              1.019,
    "Milwaukee Brewers":              1.020,
    "Washington Nationals":           1.021,
    "Chicago White Sox":              1.023,
    "Chicago Cubs":                   1.024,
    "Cleveland Guardians":            1.026,
    "Atlanta Braves":                 1.033,
    "San Francisco Giants":           1.034,
    "Houston Astros":                 1.036,
    "San Diego Padres":               1.050,
    "Oakland Athletics":              1.060,
    "Seattle Mariners":               1.078,
}


def get_team_venue_factor(team_name, home_team):
    """
    Return the team-specific home or away run factor.
    Falls back to HOME_FIELD_RUN_BOOST (home) or 1.0 (away) if team not found.
    """
    if home_team:
        return TEAM_HOME_FACTOR.get(team_name, HOME_FIELD_RUN_BOOST)
    else:
        return TEAM_AWAY_FACTOR.get(team_name, 1.0)


# ---- Calibration table (derived from 2025 backtest, 799 games, wRC+ model) ----
# Raw model probability → actual observed win rate (piecewise linear interpolation)
# New wRC+ model is slightly underconfident at 50-60% (predicts 52.5%, actual 54.0%).
# High-sample buckets (n>=262) taken directly; extremes smoothed conservatively.
#   0.525 → 0.540  (n=454, actual)
#   0.575 → 0.612  (n=262, actual 61.5%, nudged for monotonicity)
#   0.625 → 0.615  (n=44,  actual 61.4%)
#   0.675 → 0.650  (n=18,  smoothed; actual 61.1% likely noise)
#   0.725 → 0.720  (n=13,  smoothed; actual 84.6% — conservative)
#
# 2026 early-season update (95 bets, Apr 18–24):
#   65–70% range: model avg 67.6%, actual win rate 45%  → aggressively corrected
#   70%+  range: model avg 75.7%, actual win rate 52%  → corrected toward 55%
#   ERA_INPUT_CAP (7.0) reduces extreme inputs driving the highest raw probs,
#   so the upper tail correction is less severe than raw data alone suggests.
_CAL_RAW = np.array([0.00, 0.50, 0.525, 0.575, 0.625, 0.675, 0.725, 1.00])
_CAL_ADJ = np.array([0.00, 0.50, 0.540, 0.612, 0.615, 0.630, 0.650, 1.00])


def calibrate_probability(p):
    """
    Apply backtest-derived calibration to a raw model win probability.

    The wRC+ model is slightly underconfident at 50-60% and well-calibrated at 60-65%.
    This maps raw probabilities to observed win rates using piecewise interpolation.

    Symmetry is preserved: calibrate(p) + calibrate(1-p) == 1.0
    """
    if p >= 0.5:
        return float(np.interp(p, _CAL_RAW, _CAL_ADJ))
    else:
        # Mirror: calibrate the complement and flip
        return 1.0 - float(np.interp(1.0 - p, _CAL_RAW, _CAL_ADJ))


def get_park_factor(home_team, venue=None):
    """
    Return the park run factor for today's game.
    Checks alternate/neutral venue overrides first (e.g. Mexico City), then
    falls back to the home team's normal park factor.
    """
    if venue:
        override = ALTERNATE_VENUE_FACTORS.get(venue.lower().strip())
        if override is not None:
            return override
    return PARK_FACTORS.get(home_team, 1.0)


def blend_pitcher_era(starter_era, bullpen_era, pitcher_avg_ip=None):
    """
    Blend starter and bullpen ERA weighted by dynamically estimated innings.

    pitcher_avg_ip: per-pitcher blended avg IP/start (current + prior year).
      If provided, used directly as starter_ip (pitcher already implicitly
      captures ERA effect). Falls back to ERA-formula if None.

    ERA-formula fallback:
      - League-avg ERA (4.20) → 5.26 IP baseline (data: 13,811 starts 2023-2025)
      - Each +1.0 ERA above avg → 0.20 fewer starter innings
      - Capped at [3.5, 6.5] innings
    """
    if pitcher_avg_ip is not None:
        starter_ip = pitcher_avg_ip
    else:
        starter_ip = STARTER_INNINGS - STARTER_IP_PER_ERA_RUN * (starter_era - LEAGUE_AVG_XFIP)
    starter_ip = max(3.5, min(6.5, starter_ip))
    bullpen_ip = 9.0 - starter_ip
    return (starter_era * starter_ip + bullpen_era * bullpen_ip) / 9.0


# ERA ceiling fed into pitching_adj. Outlier small-sample ERAs (e.g. 14.73 in 3 IP)
# can drive the pitching multiplier to 3.5x and push win probabilities above 90%.
# Capping at 7.0 limits the worst-case ERA effect to 1.67x league average, which
# still clearly represents a poor pitcher without blowing out the probability.
ERA_INPUT_CAP = 7.0


def expected_runs(offense_wrc_plus, opp_era_blended, park_factor=1.0,
                  home_team=False, weather_factor=1.0, venue_factor=1.0):
    """
    Estimate how many runs a team is expected to score.

    offense_wrc_plus : team's wRC+ (100 = league avg, 110 = 10% above avg)
    opp_era_blended  : opposing team's blended starter+bullpen ERA estimate
    park_factor      : applied to AWAY team only — venue effect on visiting offense
                       (home team's venue effect is embedded in their home_factor)
    home_team        : True if this is the home team
    weather_factor   : temperature/wind run environment multiplier (1.0 = neutral)
    venue_factor     : team-specific home or away factor (replaces generic HOME_FIELD_RUN_BOOST)
                       home team → TEAM_HOME_FACTOR (park effect embedded)
                       away team → TEAM_AWAY_FACTOR (road tendency; park_factor applied separately)
    """
    offense_adj  = offense_wrc_plus / LEAGUE_AVG_WRC_PLUS
    pitching_adj = min(opp_era_blended, ERA_INPUT_CAP) / LEAGUE_AVG_XFIP

    if home_team:
        # Park effect embedded in venue_factor — don't apply park_factor separately
        env = venue_factor
    else:
        # Park factor for this specific venue + team's road tendency
        env = park_factor * venue_factor

    exp = (LEAGUE_AVG_RUNS * offense_adj * pitching_adj * env * weather_factor)

    return max(1.5, min(exp, 10.0))


def win_probability_poisson(home_exp_runs, away_exp_runs, max_runs=30):
    """
    Calculate home team win probability using negative binomial distributions.
    (Function name kept for compatibility; Poisson replaced with NB internally.)

    NB captures MLB's overdispersion: actual variance ~10.5 vs Poisson's ~4.4.
    Fitted r=3.14 from 4,856 team-games (2025). 0-run games: NB 6.26% vs Poisson 1.17%.

    P(home wins) = sum of P(H=h) * P(A=a) for all h > a
    Tie probability is split 50/50 (extra innings).
    """
    home_pmf = np.array([_nb_pmf(k, home_exp_runs) for k in range(max_runs + 1)])
    away_pmf = np.array([_nb_pmf(k, away_exp_runs) for k in range(max_runs + 1)])

    # Outer product: joint[h, a] = P(home=h) * P(away=a)
    joint    = np.outer(home_pmf, away_pmf)
    home_win = float(np.tril(joint, k=-1).sum())   # h > a  (below diagonal)
    tie      = float(np.trace(joint))               # h == a (diagonal)

    home_win += tie * 0.5
    return home_win


def rest_factor(days_since_last_game):
    """
    Run environment multiplier based on days since team's last game.
    Only back-to-back games get a penalty — everything else is baseline.
    """
    if days_since_last_game <= 1:  return 0.98
    return 1.00


def calculate_strikeout_projection(pitcher_k_pct, opp_team_k_pct,
                                    league_avg_k_pct=0.22, batters_faced=22):
    """
    Project expected strikeouts for a starting pitcher.

    pitcher_k_pct    : pitcher's strikeout rate (K per batter faced)
    opp_team_k_pct   : opposing team's strikeout rate as batters
    league_avg_k_pct : MLB average K% (~0.22 in recent seasons)
    batters_faced    : expected BF for a starter (~22 = ~5.5 innings × 4 BF/inn)

    Formula:
      team_k_adj = opp_team_k_pct / league_avg_k_pct
      expected_k = pitcher_k_pct * team_k_adj * batters_faced
    """
    if not pitcher_k_pct or pitcher_k_pct <= 0:
        return None

    team_k_adj = opp_team_k_pct / league_avg_k_pct if league_avg_k_pct > 0 else 1.0
    expected_k = pitcher_k_pct * team_k_adj * batters_faced
    return round(expected_k, 2)


def calculate_game_probability(home_wrc_plus, away_wrc_plus,
                                home_pitcher_era_est, away_pitcher_era_est,
                                venue="",
                                home_bullpen_era=None, away_bullpen_era=None,
                                weather_factor=1.0,
                                home_rest_days=2, away_rest_days=2,
                                away_defense_factor=1.0, home_defense_factor=1.0,
                                home_pitcher_avg_ip=None, away_pitcher_avg_ip=None,
                                home_team="", away_team=""):
    """
    Full game probability calculation.

    home/away_bullpen_era : team bullpen ERA estimate (defaults to league avg)
    weather_factor        : run environment multiplier from weather_fetcher
    home_team / away_team : team names for TEAM_HOME_FACTOR / TEAM_AWAY_FACTOR lookup

    Returns a dict with:
      home_win_prob, away_win_prob,
      home_exp_runs, away_exp_runs,
      park_factor
    """
    park_factor = get_park_factor(home_team, venue=venue)

    # Blend starter ERA with bullpen ERA (weighted by expected innings)
    home_era_blended = blend_pitcher_era(
        home_pitcher_era_est,
        home_bullpen_era if home_bullpen_era is not None else LEAGUE_AVG_XFIP,
        pitcher_avg_ip=home_pitcher_avg_ip,
    )
    away_era_blended = blend_pitcher_era(
        away_pitcher_era_est,
        away_bullpen_era if away_bullpen_era is not None else LEAGUE_AVG_XFIP,
        pitcher_avg_ip=away_pitcher_avg_ip,
    )

    home_rf = rest_factor(home_rest_days)
    away_rf = rest_factor(away_rest_days)

    home_venue_factor = get_team_venue_factor(home_team, home_team=True)
    away_venue_factor = get_team_venue_factor(away_team, home_team=False)

    # Home team scores against away pitching — park effect embedded in home_venue_factor
    home_exp = expected_runs(home_wrc_plus, away_era_blended,
                              park_factor, home_team=True,
                              weather_factor=weather_factor * home_rf * away_defense_factor,
                              venue_factor=home_venue_factor)
    # Away team scores against home pitching — park_factor applied separately
    away_exp = expected_runs(away_wrc_plus, home_era_blended,
                              park_factor, home_team=False,
                              weather_factor=weather_factor * away_rf * home_defense_factor,
                              venue_factor=away_venue_factor)

    home_exp += RUN_CALIBRATION_OFFSET
    away_exp += RUN_CALIBRATION_OFFSET

    home_prob = win_probability_poisson(home_exp, away_exp)
    home_prob = min(max(home_prob, 0.05), 0.95)  # sanity cap
    home_prob = calibrate_probability(home_prob)  # backtest calibration

    return {
        "home_win_prob":  round(home_prob, 4),
        "away_win_prob":  round(1 - home_prob, 4),
        "home_exp_runs":  round(home_exp, 2),
        "away_exp_runs":  round(away_exp, 2),
        "park_factor":    park_factor
    }


# Fraction of runs that score in innings 1-5 (empirically ~56% in MLB)
F5_RUN_FRACTION = 0.56


def calculate_f5_probability(home_wrc_plus, away_wrc_plus,
                              home_pitcher_era_est, away_pitcher_era_est,
                              venue="", weather_factor=1.0,
                              away_defense_factor=1.0, home_defense_factor=1.0,
                              home_team="",
                              away_starter_ip=5.0, home_starter_ip=5.0,
                              away_bullpen_era=4.25, home_bullpen_era=4.25):
    """
    First 5 Innings win probability and expected runs.

    Starter IP in F5 is blended: 50% static 5 IP + 50% pitcher's recent avg IP/start
    (if they have 3+ GS this season; otherwise defaults to 5.0).
    Bullpen covers the remaining F5 innings.

    Returns a dict with home_win_prob, away_win_prob, home_exp_runs, away_exp_runs.
    """
    park_factor = get_park_factor(home_team) if home_team else 1.0

    def _f5_exp(wrc, starter_era, bullpen_era, starter_ip, env, is_home):
        """Expected runs in 5 innings: starter for their IP, bullpen for the rest."""
        starter_ip   = min(max(starter_ip, 0.0), 5.0)
        bullpen_ip   = 5.0 - starter_ip
        starter_runs = expected_runs(wrc, starter_era, park_factor,
                                     home_team=is_home,
                                     weather_factor=env) * (starter_ip / 9.0)
        bullpen_runs = expected_runs(wrc, bullpen_era, park_factor,
                                     home_team=is_home,
                                     weather_factor=env) * (bullpen_ip / 9.0)
        return starter_runs + bullpen_runs

    home_f5_exp = _f5_exp(
        home_wrc_plus, away_pitcher_era_est, away_bullpen_era,
        away_starter_ip, weather_factor * away_defense_factor, is_home=True
    )
    away_f5_exp = _f5_exp(
        away_wrc_plus, home_pitcher_era_est, home_bullpen_era,
        home_starter_ip, weather_factor * home_defense_factor, is_home=False
    )

    home_prob = win_probability_poisson(home_f5_exp, away_f5_exp)
    home_prob = min(max(home_prob, 0.05), 0.95)
    home_prob = calibrate_probability(home_prob)

    return {
        "home_win_prob":  round(home_prob, 4),
        "away_win_prob":  round(1 - home_prob, 4),
        "home_exp_runs":  round(home_f5_exp, 2),
        "away_exp_runs":  round(away_f5_exp, 2),
    }


def calculate_over_probability(home_exp_runs, away_exp_runs, total_line,
                                max_runs=25):
    """
    Calculate P(total runs > total_line) using Poisson distributions.
    Handles both whole numbers and half-lines.
    """
    over_prob  = 0.0
    push_prob  = 0.0

    cutoff = int(total_line)
    is_half = (total_line != cutoff)

    home_pmf = np.array([_nb_pmf(k, home_exp_runs) for k in range(max_runs + 1)])
    away_pmf = np.array([_nb_pmf(k, away_exp_runs) for k in range(max_runs + 1)])
    joint    = np.outer(home_pmf, away_pmf)

    for h in range(max_runs + 1):
        for a in range(max_runs + 1):
            total = h + a
            p = joint[h, a]
            if total > total_line:
                over_prob += p
            elif not is_half and total == total_line:
                push_prob += p

    if is_half:
        under_prob = 1.0 - over_prob
    else:
        under_prob = 1.0 - over_prob - push_prob

    return round(over_prob, 4), round(under_prob, 4)


def calculate_runline_probabilities(home_win_prob, home_exp_runs, away_exp_runs,
                                     max_runs=30):
    """
    Estimate run-line cover probabilities for both teams and both sides.

    Returns (home_minus_prob, away_plus_prob, away_minus_prob, home_plus_prob) where:
      home_minus_prob = P(home wins by 2+)       → home -1.5 covers
      away_plus_prob  = P(away wins or loses ≤1) → away +1.5 covers
      away_minus_prob = P(away wins by 2+)       → away -1.5 covers
      home_plus_prob  = P(home wins or loses ≤1) → home +1.5 covers
    """
    home_pmf = np.array([_nb_pmf(k, home_exp_runs) for k in range(max_runs + 1)])
    away_pmf = np.array([_nb_pmf(k, away_exp_runs) for k in range(max_runs + 1)])
    joint    = np.outer(home_pmf, away_pmf)

    home_minus = 0.0
    away_minus = 0.0

    for h in range(max_runs + 1):
        for a in range(max_runs + 1):
            p = joint[h, a]
            if h - a >= 2:
                home_minus += p
            if a - h >= 2:
                away_minus += p

    away_plus = 1.0 - home_minus
    home_plus = 1.0 - away_minus
    return round(home_minus, 4), round(away_plus, 4), round(away_minus, 4), round(home_plus, 4)
