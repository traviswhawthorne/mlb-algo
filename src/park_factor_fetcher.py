"""
Park Factor Fetcher — Baseball Reference
==========================================
Fetches multi-year regressed park factors from Baseball Reference.

BR methodology:
  - Compares run environment in each team's home park vs their road games
  - Controls for team quality (same teams, home vs road)
  - Multi-year regressed to reduce noise
  - Values centered at 100 (100 = neutral, 105 = 5% more runs)

We average 3 seasons (2022-2024) and convert to multipliers (105 -> 1.05).
"""

import requests
import pandas as pd

SEASONS = [2022, 2023, 2024]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Maps Baseball Reference team abbreviations to our model's team names
BR_TEAM_TO_NAME = {
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CHW": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "OAK": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SDP": "San Diego Padres",
    "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSN": "Washington Nationals",
}


def fetch_season_park_factors(season):
    """Fetch park factors for one season from Baseball Reference."""
    url = f"https://www.baseball-reference.com/leagues/majors/{season}-park-factors.shtml"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    tables = pd.read_html(resp.text)
    if not tables:
        raise ValueError("No tables found on page")

    # Show columns from first table to help debug
    df = tables[0]
    print(f"    Columns: {list(df.columns)}")
    return df


def fetch_park_factors(seasons=SEASONS):
    """
    Fetch BR park factors for multiple seasons.
    Returns { team_name: avg_park_factor_multiplier }
    """
    team_totals = {}
    team_counts = {}

    for season in seasons:
        print(f"  Fetching {season}...")
        try:
            df = fetch_season_park_factors(season)

            # Find team column and runs park factor column
            team_col = None
            pf_col = None
            for col in df.columns:
                c = str(col).strip()
                if c in ("Tm", "Team"):
                    team_col = col
                if c in ("BasicPF", "PF", "R", "bPF"):
                    pf_col = col

            if team_col is None or pf_col is None:
                print(f"    Could not identify team/PF columns. Columns: {list(df.columns)}")
                continue

            print(f"    Using team='{team_col}', pf='{pf_col}'")

            for _, row in df.iterrows():
                abbr = str(row[team_col]).strip().upper()
                try:
                    pf = float(row[pf_col])
                except (ValueError, TypeError):
                    continue
                if abbr and abbr != "TM" and abbr != "NAN":
                    team_totals[abbr] = team_totals.get(abbr, 0.0) + pf
                    team_counts[abbr] = team_counts.get(abbr, 0) + 1

        except Exception as e:
            print(f"    Error: {e}")

    result = {}
    for abbr, total in team_totals.items():
        avg_pf = total / team_counts[abbr]       # e.g. 112.3
        multiplier = round(avg_pf / 100.0, 3)    # e.g. 1.123
        team_name = BR_TEAM_TO_NAME.get(abbr)
        if team_name:
            result[team_name] = multiplier
        else:
            print(f"  Unknown BR abbreviation: '{abbr}'")

    return result


if __name__ == "__main__":
    print("Fetching Baseball Reference park factors...\n")
    factors = fetch_park_factors()

    if not factors:
        print("\nNo factors retrieved.")
    else:
        sorted_factors = sorted(factors.items(), key=lambda x: x[1], reverse=True)
        print(f"\n{'='*55}")
        print(f"  Park Factors (Baseball Ref, avg {SEASONS[0]}-{SEASONS[-1]})")
        print(f"  1.00 = neutral | 1.05 = 5% more runs")
        print(f"{'='*55}")
        for team, pf in sorted_factors:
            print(f"  {team:<35}  {pf:.3f}")

        print(f"\n{'='*55}")
        print("  Ready to paste into model.py")
        print(f"{'='*55}")
        print("\nPARK_FACTORS = {")
        for team, pf in sorted_factors:
            print(f'    "{team}":{" " * max(1, 36-len(team))} {pf:.3f},')
        print("}")

    input("\nPress Enter to close...")
