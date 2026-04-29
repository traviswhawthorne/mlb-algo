# ============================================================
# MLB BETTING ALGORITHM — CONFIGURATION
# Edit the values below to customize your setup
# ============================================================

import os

# --- ODDS API ---
# Get your FREE key at: https://the-odds-api.com
# Free tier gives 500 requests/month (enough for a full MLB season)
# On GitHub Actions, set the ODDS_API_KEY secret instead of editing here.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "23d6c9e831180dd8136d8116fa629d90")

# --- YOUR BANKROLL ---
# Set this to your total betting bankroll in dollars
BANKROLL = 1000

# --- MODEL SETTINGS ---
# Minimum edge required to recommend a bet (2% = 0.02)
# Lower = more bets but weaker edge. Recommend keeping at 0.02 to 0.03
MIN_EV_THRESHOLD = 0.05

# Kelly fraction: 0.25 = Quarter Kelly (recommended - conservative)
# 0.50 = Half Kelly (moderate risk), 1.0 = Full Kelly (aggressive, not recommended)
KELLY_FRACTION = 0.25

# --- SEASON ---
SEASON = 2026

# --- OUTPUT ---
# On GitHub Actions, set OUTPUT_FILE=MLB_Picks.xlsx so files write to the workspace root.
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", r"G:\My Drive\MLB_Picks.xlsx")
